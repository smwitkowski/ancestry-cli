"""`familysearch`: read-only FamilySearch from your own signed-in Chrome (a second Chrome from the Ancestry one, default port 9223).

Same design as `ancestry`: no password handling and no API key. Every request is an in-page fetch inside the one open
familysearch.org tab, which sends its own session as the Authorization header; the session value never leaves the page. The tool
never opens, navigates or closes that tab. One JSON object per command; exit 0 ok, 1 failed (nothing changed), 2 bad usage.
Reads only: FamilySearch tree writes are out of scope.

  familysearch doctor
  familysearch search --surname S [--given G] [--birth Y] [--death Y] [--marriage Y] [--place P] [--collection C] [--years N] [--limit N]
  familysearch record ARK            (1:1:XXXX-XXX or the 61903/1:1:... form)
  familysearch person PID [--sources]
  familysearch film DGS              (catalog entry and image count of a digital film)
  familysearch image (--film DGS --image N | --ark 3:1:XXXX | --das TH-...) --out FILE [--crop x,y,w,h] [--max-tiles N]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import runtime as rt
from .runtime import LaneError, failure

ORIGIN = "https://www.familysearch.org"
DEFAULT_PORT = 9223
PACE = 3.0                 # seconds between API calls, across processes (the workspace lock convention)
TILE_PACE = 0.5            # seconds between image tiles (one image is many small requests)
_ARK = re.compile(r"(?:61903/)?(1:1:[A-Z0-9-]{1,80})\Z")
_IMG_ARK = re.compile(r"(?:61903/)?(3:1:[A-Z0-9-]{1,80})\Z")
_PID = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{3,4}\Z")
_DAS = re.compile(r"TH-[0-9A-Za-z-]{1,60}\Z")
_DGS = re.compile(r"[0-9]{1,12}\Z")
# the only requests this tool will make (all GET unless noted), checked before anything is sent
_ALLOWED = [
    re.compile(r"/ark:/61903/1:1:[A-Z0-9-]{1,80}\?useSLS=true&useRolesOverride=false\Z"),
    re.compile(r"/service/search/hr/v2/personas\?[^#\s]{1,3000}\Z"),
    re.compile(r"/platform/users/current\Z"),
    re.compile(r"/platform/tree/persons/[A-Z0-9-]{1,12}(?:/(?:parents|spouses|children|sources))?\Z"),
    re.compile(r"/service/records/storage/dascloud/das/v2/TH-[0-9A-Za-z-]{1,60}/(?:permission|thumb_p200\.jpg)\Z"),
    re.compile(r"/service/records/storage/deepzoomcloud/dz/v1/TH-[0-9A-Za-z-]{1,60}/image\.xml\Z"),
    re.compile(r"/service/records/storage/deepzoomcloud/dz/v1/TH-[0-9A-Za-z-]{1,60}/image_files/[0-9]{1,2}/[0-9]{1,3}_[0-9]{1,3}\.jpg\Z"),
]
_ALLOWED_POST = {"/search/filmdatainfo/film-data", "/search/filmdatainfo/image-data"}


def port():
    return int(os.environ.get("FAMILYSEARCH_CLI_PORT") or DEFAULT_PORT)


def lock_file():
    override = os.environ.get("FAMILYSEARCH_CLI_LOCK_FILE")
    if override:
        return Path(override)
    import tempfile
    return Path(tempfile.gettempdir()) / f"familysearch-cli-locks-{os.getuid()}" / f"port-{port()}.lock"


# ------------------------------------------------------------------------------------------------ in-page transport
_FETCH_JS = r"""(async (spec) => {
  const cookie = (document.cookie || '').split('; ').find(c => c.startsWith('fssessionid='));
  const tok = cookie ? cookie.slice('fssessionid='.length) : '';
  const clean = t => tok ? t.split(tok).join('<session>') : t;
  const out = [];
  for (let i = 0; i < spec.calls.length; i++) {
    const c = spec.calls[i];
    if (i && spec.pause) await new Promise(r => setTimeout(r, spec.pause));
    const headers = {'Accept': c.accept, 'X-Requested-With': 'XMLHttpRequest'};
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    let body;
    if (c.body) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(c.body).split('__SESSION__').join(tok); }
    try {
      const r = await fetch(c.path, {method: c.method, credentials: 'include', cache: 'no-store', redirect: 'manual', headers, body});
      const ctype = r.headers.get('content-type') || '';
      const row = {status: r.status, type: ctype, redirected: r.type === 'opaqueredirect', signed_in: !!tok};
      if (c.binary) {
        const b = new Uint8Array(await r.arrayBuffer()); let s = '';
        for (let k = 0; k < b.length; k += 0x8000) s += String.fromCharCode.apply(null, b.subarray(k, k + 0x8000));
        row.b64 = btoa(s);
      } else { row.text = clean(await r.text()); }
      out.push(row);
    } catch (e) { out.push({status: 0, error: 'fetch-failed', signed_in: !!tok}); }
  }
  return JSON.stringify({page: location.origin, calls: out});
})"""


def _check(call):
    path, method = call["path"], call.get("method", "GET")
    ok = path in _ALLOWED_POST if method == "POST" else any(p.fullmatch(path) for p in _ALLOWED)
    if not ok:
        raise ValueError("path-not-allowed")


async def _evaluate(lease, calls, pause):
    import websockets
    version = json.load(urllib.request.urlopen(f"{lease.base}/json/version", timeout=10))
    async with websockets.connect(version["webSocketDebuggerUrl"], max_size=64 * 2**20, open_timeout=15) as ws:
        counter = 0

        async def call(method, params=None, session=None):
            nonlocal counter
            counter += 1
            message = {"id": counter, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            await ws.send(json.dumps(message))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == counter:
                    return reply
        sid = (await call("Target.attachToTarget", {"targetId": lease.target_id, "flatten": True}))["result"]["sessionId"]
        try:
            expr = "(" + _FETCH_JS + ")(" + json.dumps({"calls": calls, "pause": int(pause * 1000)}) + ")"
            reply = await call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True,
                                                    "userGesture": False}, sid)
        finally:
            await call("Target.detachFromTarget", {"sessionId": sid})
    value = (reply.get("result") or {}).get("result", {}).get("value")
    if not isinstance(value, str):
        raise LaneError("native-operation-failed")
    return json.loads(value)


def _lease():
    return rt.Lease(_Bridge(), origin=ORIGIN, port=port())


class _Bridge:
    """Just enough of the bridge for Lease: the CDP page list."""
    @staticmethod
    def _cdp_json(base, path):
        return json.load(urllib.request.urlopen(base + path, timeout=10))


def fetch(calls, *, pause=0.0):
    """Run read calls inside the FamilySearch tab under the shared lock. Returns the list of rows.
    Raises LaneError with a stable classification; nothing here opens, navigates or closes a tab."""
    for c in calls:
        _check(c)
    with rt.quiet(), rt.lock("familysearch", path=lock_file(), interval=PACE):
        lease = _lease().check()
        result = asyncio.run(_evaluate(lease, calls, pause))
        lease.check()
    if result.get("page") != ORIGIN:
        raise LaneError("familysearch-tab-required")
    rows = result["calls"]
    for row in rows:
        _classify(row)
    return rows


def _classify(row):
    status = row.get("status")
    text = row.get("text") or ""
    if status == 0:
        raise LaneError("familysearch-unavailable", status=0)
    if status in (401, 403) or (status == 200 and "Just a moment" in text[:2000]):
        if "Just a moment" in text[:2000] or "cf-" in text[:2000].lower() or row.get("type", "").startswith("text/html") and status == 403:
            raise LaneError("familysearch-check-required", status=status)
        raise LaneError("familysearch-sign-in-required", status=status)
    if row.get("redirected") or status in (301, 302, 303, 307):
        raise LaneError("familysearch-sign-in-required", status=status)
    if status == 429:
        raise LaneError("familysearch-rate-limited", status=429)
    if status == 404:
        raise LaneError("familysearch-not-found", status=404)
    if not (isinstance(status, int) and 200 <= status < 300):
        raise LaneError("familysearch-unavailable", status=status)


def _json(row):
    try:
        return json.loads(row["text"])
    except (KeyError, ValueError):
        raise LaneError("familysearch-unavailable", status=row.get("status")) from None


def _guard(fn):
    try:
        return fn()
    except LaneError as exc:
        return failure(exc.code, **exc.detail)
    except ValueError:
        return failure("invalid-request")
    except (OSError, KeyError, TypeError, IndexError):
        return failure("familysearch-unavailable")
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")


# ------------------------------------------------------------------------------------------------ projections
def _fact(f):
    date = (f.get("date") or {}).get("original")
    place = (f.get("place") or {}).get("original")
    return {"type": str(f.get("type", "")).rsplit("/", 1)[-1], "date": date, "place": place}


def _person(p):
    names = [n for n in p.get("names", []) if n.get("nameForms")]
    full = names[0]["nameForms"][0].get("fullText") if names else None
    ids = (p.get("identifiers") or {}).get("http://gedcomx.org/Persistent") or []
    ark = next((m.group(1) for i in ids for m in [re.search(r"61903/(1:1:[A-Z0-9-]+)", i)] if m), None)
    role = next((v.get("text") for f in p.get("fields", []) if str(f.get("type", "")).endswith("Role") for v in f.get("values", [])), None)
    return {"name": full, "gender": str((p.get("gender") or {}).get("type", "")).rsplit("/", 1)[-1] or None, "principal": bool(p.get("principal")),
            "role": role, "ark": ark, "facts": [_fact(f) for f in p.get("facts", [])]}


def _collection(sds):
    return next(({"id": str(sd.get("about", "")).rsplit("/", 1)[-1], "title": (sd.get("titles") or [{}])[0].get("value")}
                 for sd in sds if str(sd.get("resourceType", "")).endswith("Collection")), None)


def search(*, surname, given=None, birth=None, death=None, marriage=None, place=None, collection=None, years=2, limit=10):
    if not (surname or given):
        return failure("missing-arguments")
    q = {"q.surname": surname}
    if given:
        q["q.givenName"] = given
    for event, year in (("birth", birth), ("death", death), ("marriage", marriage)):
        if year:
            q[f"q.{event}LikeDate.from"], q[f"q.{event}LikeDate.to"] = str(int(year) - years), str(int(year) + years)
    if place:
        q["q.anyPlace"] = place
    if collection:
        q["q.collectionId"] = str(int(collection))
    q.update({"count": str(max(1, min(int(limit), 50))), "offset": "0", "m.defaultFacets": "on",
              "m.facetNestCollectionInCategory": "on", "m.queryRequireDefault": "on"})

    def run():
        row, = fetch([{"method": "GET", "path": "/service/search/hr/v2/personas?" + urllib.parse.urlencode(q), "accept": "application/json"}])
        data = _json(row)
        hits = []
        for e in data.get("entries", []):
            g = (e.get("content") or {}).get("gedcomx") or {}
            persons = [_person(p) for p in g.get("persons", [])]
            main = next((p for p in persons if p["principal"]), persons[0] if persons else {})
            hits.append({"ark": main.get("ark"), "score": e.get("score"), "confidence": e.get("confidence"),
                         "collection": _collection(g.get("sourceDescriptions", [])), "principal": main,
                         "others": [{k: p[k] for k in ("name", "role", "ark")} for p in persons if p is not main][:8]})
        return {"ok": True, "classification": "familysearch-search", "dispatch_attempted": True, "state": "unchanged",
                "total": data.get("results"), "returned": len(hits), "hits": hits}
    return _guard(run)


def record(ark):
    m = _ARK.match(str(ark))
    if not m:
        return failure("invalid-request", problems=[{"field": "ark", "issue": "invalid", "expected": "1:1:XXXX-XXX"}])
    short = m.group(1)

    def run():
        row, = fetch([{"method": "GET", "path": f"/ark:/61903/{short}?useSLS=true&useRolesOverride=false",
                       "accept": "application/x-gedcomx-v1+json"}])
        g = _json(row)
        fields = {}
        for f in g.get("fields", []):
            label = str(f.get("type", "")).rsplit("/", 1)[-1]
            values = [v.get("text") for v in f.get("values", []) if v.get("text")]
            if values and label != "ExtData":
                fields[label] = values[0] if len(values) == 1 else values
        sds = g.get("sourceDescriptions", [])
        cite = next((c.get("value") for sd in sds if str(sd.get("about", "")).endswith(short) for c in sd.get("citations", [])), None)
        people = [_person(p) for p in g.get("persons", [])]
        by_id = {p.get("id"): _person(p) for p in g.get("persons", [])}
        rels = []
        for r in g.get("relationships", []):
            a, b = (by_id.get(str((r.get(k) or {}).get("resource", "")).lstrip("#")) for k in ("person1", "person2"))
            rels.append({"type": str(r.get("type", "")).rsplit("/", 1)[-1], "person1": a and a["name"], "person2": b and b["name"]})
        return {"ok": True, "classification": "familysearch-record", "dispatch_attempted": True, "state": "unchanged", "ark": short,
                "collection": _collection(sds), "citation": re.sub(r"</?i>", "", cite) if cite else None, "fields": fields,
                "persons": people, "relationships": rels}
    return _guard(run)


def person(pid, *, sources=False):
    if not _PID.match(str(pid)):
        return failure("invalid-request", problems=[{"field": "pid", "issue": "invalid", "expected": "like KWQS-BBQ"}])
    base = f"/platform/tree/persons/{pid}"
    gx = "application/x-gedcomx-v1+json"
    paths = [base, base + "/parents", base + "/spouses", base + "/children"] + ([base + "/sources"] if sources else [])

    def run():
        rows = fetch([{"method": "GET", "path": p, "accept": gx} for p in paths])
        main, parents, spouses, children, *rest = [_json(r) for r in rows]

        def find(data, pid_):
            return next((p for p in data.get("persons", []) if p.get("id") == pid_), {})

        def brief(p):
            disp = p.get("display") or {}
            return {"pid": p.get("id"), "name": disp.get("name"), "lifespan": disp.get("lifespan"), "gender": disp.get("gender")}

        def relatives(data):
            return [brief(p) for p in data.get("persons", []) if p.get("id") != pid]
        me = find(main, pid)
        out = {"ok": True, "classification": "familysearch-person", "dispatch_attempted": True, "state": "unchanged", "pid": pid,
               "name": (me.get("display") or {}).get("name"), "lifespan": (me.get("display") or {}).get("lifespan"),
               "gender": (me.get("display") or {}).get("gender"), "living": bool(me.get("living")),
               "facts": [{**_fact(f), "id": f.get("id")} for f in me.get("facts", [])],
               "parents": relatives(parents), "spouses": relatives(spouses), "children": relatives(children)}
        if sources:
            sds = rest[0].get("sourceDescriptions", [])
            out["sources"] = [{"title": (sd.get("titles") or [{}])[0].get("value"), "about": sd.get("about"),
                               "citation": (sd.get("citations") or [{}])[0].get("value")} for sd in sds]
        return out
    return _guard(run)


def _film_body(dgs):
    return {"type": "film-data", "loggedIn": True, "sessionId": "__SESSION__",
            "args": {"dgsNum": dgs, "state": {"cc": None, "imageOrFilmUrl": f"/search/film/{dgs}", "collectionContext": None, "viewMode": "g"},
                     "locale": "en"}}


def film(dgs):
    if not _DGS.match(str(dgs)):
        return failure("invalid-request", problems=[{"field": "dgs", "issue": "invalid", "expected": "digits"}])

    def run():
        row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/film-data", "accept": "application/json", "body": _film_body(dgs)}])
        data = _json(row)
        cat = ((data.get("catalogs") or [{}])[0]).get("data") or {}
        notes = [{"film": n.get("filmno"), "dgs": n.get("digital_film_no"), "contents": n.get("text"), "items": n.get("items") or None}
                 for n in cat.get("film_note", [])]
        return {"ok": True, "classification": "familysearch-film", "dispatch_attempted": True, "state": "unchanged", "dgs": str(dgs),
                "title": cat.get("display_title"), "dates": cat.get("inclusive_dates"), "format": cat.get("format"),
                "place": next(((s or {}).get("text") for s in cat.get("subjectLocality") or []), None),
                "images": len(data.get("images") or []), "film_notes": notes}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ images
def _viewable(permission):
    """The permission endpoint answers with a colon list such as `A:ThemisPrmAnyone:B`; `Anyone` in it means any signed-in user may view."""
    return bool(set(permission.split(":")) & {"ThemisPrmAnyone", "ThemisPrmSignedInUser", "ThemisPrmMember"})


def _levels(width, height):
    return math.ceil(math.log2(max(width, height, 1)))


def _plan(width, height, crop, max_tiles):
    """Pick the deepest zoom level whose tiles for the crop fit in max_tiles. Returns (level, x0, y0, x1, y1, scale) in level pixels."""
    top = _levels(width, height)
    cx, cy, cw, ch = crop or (0, 0, width, height)
    for level in range(top, -1, -1):
        scale = 2 ** (top - level)
        x0, y0, x1, y1 = int(cx // scale), int(cy // scale), math.ceil((cx + cw) / scale), math.ceil((cy + ch) / scale)
        tiles = (math.ceil(x1 / 256) - x0 // 256) * (math.ceil(y1 / 256) - y0 // 256)
        if tiles <= max_tiles:
            return level, x0, y0, x1, y1, scale
    return 0, 0, 0, 1, 1, 2 ** top


def _crop_box(spec, width, height):
    x, y, w, h = (float(v) for v in spec.split(","))
    if max(x, y, w, h) <= 1:
        x, y, w, h = x * width, y * height, w * width, h * height
    if not (0 <= x < width and 0 <= y < height and w > 0 and h > 0 and x + w <= width + 1 and y + h <= height + 1):
        raise ValueError("crop")
    return x, y, w, h


def image(*, out, film_dgs=None, number=None, ark=None, das=None, crop=None, max_tiles=36):
    from PIL import Image
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir():
        return failure("invalid-request", problems=[{"field": "out", "issue": "invalid", "expected": "a new file in an existing folder"}])
    if not (das or ark or (film_dgs and number)):
        return failure("missing-arguments")
    if das and not _DAS.match(das) or ark and not _IMG_ARK.match(ark) or film_dgs and not _DGS.match(str(film_dgs)):
        return failure("invalid-request")
    try:
        if crop:
            [float(v) for v in crop.split(",")][3]
    except (ValueError, IndexError):
        return failure("invalid-request", problems=[{"field": "crop", "issue": "invalid", "expected": "x,y,w,h"}])

    def run():
        image_ark = _IMG_ARK.match(ark).group(1) if ark else None
        if film_dgs and number and not das and not image_ark:
            row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/film-data", "accept": "application/json", "body": _film_body(str(film_dgs))}])
            images = _json(row).get("images") or []
            if not 1 <= int(number) <= len(images):
                raise LaneError("familysearch-not-found", status=404)
            image_ark = re.search(r"3:1:[A-Z0-9-]+", images[int(number) - 1]).group(0)
        das_id = das
        if not das_id:
            body = {"type": "image-data", "args": {"imageURL": f"https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/{image_ark}/image.xml",
                                                  "locale": "en", "state": {"imageOrFilmUrl": "", "selectedImageIndex": -1, "viewMode": "i"}}}
            row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/image-data", "accept": "application/json", "body": body}])
            href = (((_json(row).get("meta") or {}).get("links") or {}).get("image-deepzoom") or {}).get("href", "")
            m = re.search(r"/(TH-[0-9A-Za-z-]+)/image\.xml", href)
            if not m:
                raise LaneError("familysearch-not-found", status=404)
            das_id = m.group(1)
        d = f"/service/records/storage/deepzoomcloud/dz/v1/{das_id}"
        perm, meta = fetch([{"method": "GET", "path": f"/service/records/storage/dascloud/das/v2/{das_id}/permission", "accept": "text/plain"},
                            {"method": "GET", "path": f"{d}/image.xml", "accept": "application/xml"}])
        permission = (perm.get("text") or "").strip()
        if permission and not _viewable(permission):
            raise LaneError("familysearch-image-restricted", permission=permission[:60])
        size = re.search(r'Width="(\d+)"\s+Height="(\d+)"', meta.get("text") or "")
        if not size:
            raise LaneError("familysearch-unavailable", status=meta.get("status"))
        width, height = int(size.group(1)), int(size.group(2))
        box = _crop_box(crop, width, height) if crop else None
        level, x0, y0, x1, y1, scale = _plan(width, height, box, max_tiles)
        cols = range(x0 // 256, math.ceil(x1 / 256))
        rows_ = range(y0 // 256, math.ceil(y1 / 256))
        coords = [(c, r) for r in rows_ for c in cols]
        calls = [{"method": "GET", "path": f"{d}/image_files/{level}/{c}_{r}.jpg", "accept": "image/jpeg", "binary": True} for c, r in coords]
        tiles = []
        for start in range(0, len(calls), 12):          # a lock per batch, so other tools can interleave on long images
            tiles += fetch(calls[start:start + 12], pause=TILE_PACE)
        level_w, level_h = math.ceil(width / scale), math.ceil(height / scale)
        canvas = Image.new("RGB", (level_w, level_h))
        for (c, r), row in zip(coords, tiles):
            tile = Image.open(io.BytesIO(base64.b64decode(row["b64"]))).convert("RGB")
            canvas.paste(tile, (c * 256 - (1 if c else 0), r * 256 - (1 if r else 0)))
        if box:
            bx, by, bw, bh = box
            canvas = canvas.crop((round(bx / scale), round(by / scale), min(level_w, round((bx + bw) / scale)), min(level_h, round((by + bh) / scale))))
        canvas.save(dest, format="PNG" if dest.suffix.lower() == ".png" else "JPEG", quality=92)
        dest.chmod(0o600)
        return {"ok": True, "classification": "familysearch-image", "dispatch_attempted": True, "state": "unchanged", "file": str(dest),
                "das": das_id, "full_size": [width, height], "zoom_level": level, "reduction": scale, "saved_size": list(canvas.size),
                "tiles": len(coords), "note": "saved_size is at the zoom level used; raise --max-tiles for more detail or crop tighter."}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ doctor and CLI
def doctor():
    out = {"ok": False, "classification": "doctor", "port": port(), "lock_file": str(lock_file()), "checks": {}}
    checks = out["checks"]
    try:
        _lease().check()
        checks["browser_tab"] = "ok"
    except LaneError as exc:
        checks["browser_tab"] = exc.code
        code = {"browser-lane-not-running": "familysearch-chrome-not-running", "browser-lane-site-required": "familysearch-tab-required",
                "browser-lane-ambiguous": "familysearch-tab-ambiguous"}.get(exc.code, exc.code)
        return {**out, **failure(code), "classification": code, "checks": checks, "port": port(), "lock_file": out["lock_file"]}
    try:
        fetch([{"method": "GET", "path": "/platform/users/current", "accept": "application/json"}])
        checks["signed_in"] = "ok"
    except LaneError as exc:
        checks["signed_in"] = exc.code
        return {**out, **failure(exc.code, **exc.detail), "checks": checks, "port": port(), "lock_file": out["lock_file"]}
    out["ok"] = True
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="familysearch", description="Read-only FamilySearch through your own signed-in Chrome.")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="check the Chrome tab and the sign-in")
    s = subs.add_parser("search", help="historical record search")
    s.add_argument("--surname", required=True)
    s.add_argument("--given")
    for name in ("birth", "death", "marriage", "collection"):
        s.add_argument("--" + name, type=int)
    s.add_argument("--place", help="any place (birth, death, residence, marriage)")
    s.add_argument("--years", type=int, default=2, help="plus or minus years around the event year (default 2)")
    s.add_argument("--limit", type=int, default=10)
    r = subs.add_parser("record", help="one historical record by ARK")
    r.add_argument("ark")
    t = subs.add_parser("person", help="a family-tree person: facts, parents, spouses, children")
    t.add_argument("pid")
    t.add_argument("--sources", action="store_true", help="also list the sources attached to the person")
    f = subs.add_parser("film", help="catalog entry and image count of a digital film (DGS number)")
    f.add_argument("dgs")
    i = subs.add_parser("image", help="save a record image, optionally cropped")
    i.add_argument("--film", dest="film_dgs", help="digital film (DGS) number, with --image")
    i.add_argument("--image", dest="number", type=int, help="image number on the film, starting at 1")
    i.add_argument("--ark", help="image ARK 3:1:XXXX")
    i.add_argument("--das", help="image id TH-...")
    i.add_argument("--out", required=True)
    i.add_argument("--crop", help="x,y,w,h in full-size pixels, or fractions when all are 1 or less")
    i.add_argument("--max-tiles", type=int, default=36, help="most tiles to fetch (more tiles = sharper, slower)")
    return p


def main(argv=None):
    parser = build_parser()
    try:
        args = vars(parser.parse_args(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        print(json.dumps(rt.annotate(failure("usage-error"))))
        return 2
    command = args.pop("command")
    if command == "doctor":
        result = doctor()
    elif command == "search":
        result = search(**args)
    elif command == "record":
        result = record(args["ark"])
    elif command == "person":
        result = person(args["pid"], sources=args["sources"])
    elif command == "film":
        result = film(args["dgs"])
    else:
        result = image(out=args["out"], film_dgs=args["film_dgs"], number=args["number"], ark=args["ark"], das=args["das"],
                       crop=args["crop"], max_tiles=args["max_tiles"])
    result = rt.annotate(result)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
