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
  familysearch page (--ark 3:1:X | --film DGS --image N)   (every indexed record on one image)
  familysearch fulltext --q TEXT [--place P] [--from Y] [--to Y] [--type deed|will|probate|...] [--collection C] [--limit N] [--offset N] [--full]
  familysearch catalog --place P [--subject-id ID] [--years A-B] [--films] [--exact]   (what the catalog holds for a place)
  familysearch locate --film DGS [--type baptisms|marriages|burials|births|deaths] (--date YYYY[-MM[-DD]] | --years A-B) [--name N] [--probes K]
  familysearch film DGS --sheet --from N --to M --step K --out FILE   (contact sheet)
  familysearch waypoints --collection C [--waypoint ID] [--query WORDS]   (browse state, county, district to images)
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
    re.compile(r"/service/search/fulltext/search\?[A-Za-z0-9.%+=&_,'-]{1,800}\Z"),
    re.compile(r"/service/search/catalog/v3/search\?[A-Za-z0-9.%+=&_-]{1,600}\Z"),
    re.compile(r"/service/search/catalog/item/[a-z]{2,8}:[0-9]{1,12}\Z"),
    re.compile(r"/platform/tree/persons/[A-Z0-9-]{1,12}(?:/(?:parents|spouses|children|sources))?\Z"),
    re.compile(r"/service/records/storage/dascloud/das/v2/(?:TH-[0-9A-Za-z-]{1,60}|3:1:[A-Z0-9-]{1,60})/(?:permission|thumb_p200\.jpg)\Z"),
    re.compile(r"/service/records/storage/deepzoomcloud/dz/v1/(?:TH-[0-9A-Za-z-]{1,60}|3:1:[A-Z0-9-]{1,60})/image\.xml\Z"),
    re.compile(r"/service/records/storage/deepzoomcloud/dz/v1/(?:TH-[0-9A-Za-z-]{1,60}|3:1:[A-Z0-9-]{1,60})/image_files/[0-9]{1,2}/[0-9]{1,3}_[0-9]{1,3}\.jpg\Z"),
    re.compile(r"/service/cds/recapi/collections/[0-9]{1,12}/waypoints\Z"),
    re.compile(r"/service/cds/recapi/waypoints/[A-Z0-9-]{1,20}:[0-9,]{1,200}\?cc=[0-9]{1,12}\Z"),
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
      const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 45000);       // no request may stall the whole command
      const r = await fetch(c.path, {method: c.method, credentials: 'include', cache: 'no-store', redirect: 'manual', headers, body, signal: ctl.signal});
      clearTimeout(timer);
      const ctype = r.headers.get('content-type') || '';
      const row = {status: r.status, type: ctype, redirected: r.type === 'opaqueredirect', signed_in: !!tok};
      if (c.binary) {
        const b = new Uint8Array(await r.arrayBuffer()); let s = '';
        for (let k = 0; k < b.length; k += 0x8000) s += String.fromCharCode.apply(null, b.subarray(k, k + 0x8000));
        row.b64 = btoa(s);
      } else { row.text = clean(await r.text()); }
      out.push(row);
    } catch (e) { out.push({status: 0, error: (e && e.name === 'AbortError') ? 'timeout' : 'fetch-failed', signed_in: !!tok}); }
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
        budget = 60 + len(calls) * (pause + 50)          # each call is cut off at 45 s in the page; this bounds the whole command
        try:
            result = asyncio.run(asyncio.wait_for(_evaluate(lease, calls, pause), budget))
        except TimeoutError:
            raise LaneError("familysearch-timeout") from None
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
        raise LaneError("familysearch-timeout" if row.get("error") == "timeout" else "familysearch-unavailable", status=0)
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


def _image_arks(g):
    """Image ARKs (3:1:...) a record links to: its digital artifacts, and the IMAGE_ARK in its extra data."""
    found = []
    for sd in g.get("sourceDescriptions", []):
        if str(sd.get("resourceType", "")).endswith("DigitalArtifact"):
            found += re.findall(r"3:1:[A-Z0-9-]+", str(sd.get("about", "")))
    for f in g.get("fields", []):
        for v in f.get("values", []):
            if v.get("labelId") == "EXT_DATA" and "IMAGE_ARK" in str(v.get("text", "")):
                found += re.findall(r"IMAGE_ARK\W+[^\"]*?(3:1:[A-Z0-9-]+)", str(v["text"]))
    return list(dict.fromkeys(found))


def _record_row(short):
    row, = fetch([{"method": "GET", "path": f"/ark:/61903/{short}?useSLS=true&useRolesOverride=false",
                   "accept": "application/x-gedcomx-v1+json"}])
    return _json(row)


def record(ark):
    m = _ARK.match(str(ark))
    if not m:
        return failure("invalid-request", problems=[{"field": "ark", "issue": "invalid", "expected": "1:1:XXXX-XXX"}])
    short = m.group(1)

    def run():
        g = _record_row(short)
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
                "collection": _collection(sds), "citation": re.sub(r"</?i>", "", cite) if cite else None, "image_arks": _image_arks(g), "fields": fields,
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


def _pad(dgs):
    """FamilySearch's film service wants nine digits; catalog entries print DGS numbers unpadded (4268340 is 004268340)."""
    text = str(dgs)
    return text.zfill(9) if text.isdigit() and len(text) < 9 else text


def _film_body(dgs):
    dgs = _pad(dgs)
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
        raw = cat.get("film_note") or []
        raw = [raw] if isinstance(raw, (dict, str)) else raw
        notes = [{"film": n.get("filmno"), "dgs": n.get("digital_film_no"), "contents": n.get("text"), "items": n.get("items") or None}
                 if isinstance(n, dict) else {"film": None, "dgs": None, "contents": str(n), "items": None} for n in raw]
        return {"ok": True, "classification": "familysearch-film", "dispatch_attempted": True, "state": "unchanged", "dgs": _pad(dgs),
                "title": cat.get("display_title"), "dates": cat.get("inclusive_dates"), "format": cat.get("format"),
                "place": next(((s or {}).get("text") for s in cat.get("subjectLocality") or []), None),
                "images": len(data.get("images") or []), "film_notes": notes}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ catalog
_PLACE = re.compile(r"[\w .,'()-]{2,120}\Z")


def _catalog_years(title):
    m = re.search(r"(\d{3,4})\s*-\s*(\d{3,4})", title or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def catalog(*, place, subject_id=None, years=None, limit=10, films=False, exact=False):
    """What the FamilySearch catalog holds for a place: record types (subjects), then titles, then films with their DGS numbers."""
    if not _PLACE.match(place or ""):
        return failure("invalid-request", problems=[{"field": "place", "issue": "invalid", "expected": "a place name"}])
    span = None
    if years:
        m = re.fullmatch(r"(\d{3,4})(?:-(\d{3,4}))?", years)
        if not m:
            return failure("invalid-request", problems=[{"field": "years", "issue": "invalid", "expected": "1850 or 1850-1900"}])
        span = (int(m.group(1)), int(m.group(2) or m.group(1)))
    base = {"count": "50" if subject_id else "20", "offset": "0", "m.defaultFacets": "on", "m.queryRequireDefault": "on", "q.place": place}
    if exact:
        base["q.place.exact"] = "on"

    def run():
        if not subject_id:
            q = {**base, "groupBy": "placeSubject"}
            row, = fetch([{"method": "GET", "path": "/service/search/catalog/v3/search?" + urllib.parse.urlencode(q), "accept": "application/json"}])
            data = _json(row)
            subjects = [{"subject_id": h["metadataHit"]["metadata"]["identifier"]["value"],
                         "subject": ((h["metadataHit"]["metadata"].get("title") or [{}])[0].get("value") or "").strip()}
                        for h in data.get("searchHits", [])]
            return {"ok": True, "classification": "familysearch-catalog", "dispatch_attempted": True, "state": "unchanged", "place": place,
                    "total": data.get("totalHits"), "place_set_id": data.get("placeSetId"), "subjects": subjects,
                    "next": "familysearch catalog --place PLACE --subject-id ID [--years A-B] [--films]"}
        if not re.fullmatch(r"[0-9]{1,12}", str(subject_id)):
            raise ValueError("subject")
        q = {**base, "q.subjectId": str(subject_id)}
        row, = fetch([{"method": "GET", "path": "/service/search/catalog/v3/search?" + urllib.parse.urlencode(q), "accept": "application/json"}])
        data = _json(row)
        items = []
        for h in data.get("searchHits", []):
            md = h["metadataHit"]["metadata"]
            title = (md.get("title") or [{}])[0].get("value")
            item = str((md.get("identifier") or {}).get("value", "")).rsplit("/", 1)[-1]
            rng = _catalog_years(title)
            if span and rng and (rng[1] < span[0] or rng[0] > span[1]):
                continue
            items.append({"item": item, "title": title, "creator": (md.get("creator") or [None])[0], "years": list(rng) if rng else None,
                          "online": any(c.get("title") == "Online" for c in md.get("repositoryCalls", []))})
        items = items[:max(1, min(int(limit), 30))]
        if films and items:
            rows = []
            for start in range(0, len(items), 6):
                rows += fetch([{"method": "GET", "path": f"/service/search/catalog/item/{i['item']}", "accept": "application/json"}
                               for i in items[start:start + 6]])
            for item, r in zip(items, rows):
                notes = (_json(r).get("source") or {}).get("film_note") or []
                notes = [n for n in ([notes] if isinstance(notes, dict) else notes) if isinstance(n, dict)]
                seen, out = set(), []
                for n in notes:
                    film = {"film": str(n.get("filmno") or "") or None, "dgs": _pad(n.get("digital_film_no")) if n.get("digital_film_no") else None,
                            "contents": n.get("text"), "items": n.get("items") or None}
                    key = json.dumps(film, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        out.append(film)
                item["films"] = out
        return {"ok": True, "classification": "familysearch-catalog", "dispatch_attempted": True, "state": "unchanged", "place": place,
                "subject_id": str(subject_id), "total": data.get("totalHits"), "returned": len(items), "titles": items}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ images
def _viewable(permission):
    """The permission endpoint answers with a colon list such as `A:ThemisPrmAnyone:B`; `Anyone` in it means any signed-in user may view."""
    return bool(set(permission.split(":")) & {"ThemisPrmAnyone", "ThemisPrmSignedInUser", "ThemisPrmMember", "ThemisPrmRegisteredPatron"})


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


def image(*, out, film_dgs=None, number=None, ark=None, das=None, crop=None, max_tiles=36, record_ark=None):
    from PIL import Image
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir():
        return failure("invalid-request", problems=[{"field": "out", "issue": "invalid", "expected": "a new file in an existing folder"}])
    if not (das or ark or record_ark or (film_dgs and number)):
        return failure("missing-arguments")
    if record_ark and not _ARK.match(record_ark):
        return failure("invalid-request", problems=[{"field": "record", "issue": "invalid", "expected": "1:1:XXXX-XXX"}])
    if das and not _DAS.match(das) or ark and not _IMG_ARK.match(ark) or film_dgs and not _DGS.match(str(film_dgs)):
        return failure("invalid-request")
    try:
        if crop:
            [float(v) for v in crop.split(",")][3]
    except (ValueError, IndexError):
        return failure("invalid-request", problems=[{"field": "crop", "issue": "invalid", "expected": "x,y,w,h"}])

    def run():
        image_ark = _IMG_ARK.match(ark).group(1) if ark else None
        if record_ark and not (das or image_ark):
            g = _record_row(_ARK.match(record_ark).group(1))
            arks = _image_arks(g)
            if not arks:
                film_no = next((v.get("text") for f in g.get("fields", []) if str(f.get("type", "")).endswith("DigitalFilmNumber")
                                for v in f.get("values", [])), None)
                raise LaneError("familysearch-no-image-link", digital_film=film_no or "")
            image_ark = arks[0]
        if film_dgs and number and not das and not image_ark:
            row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/film-data", "accept": "application/json", "body": _film_body(str(film_dgs))}])
            images = _json(row).get("images") or []
            if not 1 <= int(number) <= len(images):
                raise LaneError("familysearch-not-found", status=404)
            image_ark = re.search(r"3:1:[A-Z0-9-]+", images[int(number) - 1]).group(0)
        das_id = das or image_ark          # tiles and permission answer to the image ARK as well as to the TH- id
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


# ------------------------------------------------------------------------------------------------ everything indexed on one image
def page_records(*, ark=None, film_dgs=None, number=None):
    """Every indexed record on one image (names, roles, events): a census page's household neighbors, a register page's entries."""
    if ark and not _IMG_ARK.match(ark) or film_dgs and not _DGS.match(str(film_dgs)) or not (ark or (film_dgs and number)):
        return failure("invalid-request", problems=[{"field": "ark", "issue": "invalid", "expected": "3:1:XXXX, or --film with --image"}])

    def run():
        image_ark = _IMG_ARK.match(ark).group(1) if ark else None
        if not image_ark:
            _, arks = _film_data(film_dgs)
            if not 1 <= int(number) <= len(arks):
                raise LaneError("familysearch-not-found", status=404)
            image_ark = arks[int(number) - 1]
        body = {"type": "image-data", "args": {"imageURL": f"https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/{image_ark}/image.xml",
                                              "locale": "en", "state": {"imageOrFilmUrl": "", "selectedImageIndex": -1, "viewMode": "i"}}}
        row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/image-data", "accept": "application/json", "body": body}])
        data = _json(row)
        records = []
        for rec in data.get("records") or []:
            people = [_person(p) for p in rec.get("persons", [])]
            main = next((p for p in people if p["principal"]), people[0] if people else {})
            ark_ = next((m.group(0) for p in rec.get("persons", []) for i in (p.get("identifiers") or {}).get("http://gedcomx.org/Persistent", [])
                         for m in [re.search(r"1:1:[A-Z0-9-]+", i)] if m), None)
            records.append({"ark": main.get("ark") or ark_, "principal": main, "others": [{k: p[k] for k in ("name", "role")} for p in people if p is not main][:12]})
        return {"ok": True, "classification": "familysearch-page", "dispatch_attempted": True, "state": "unchanged", "image_ark": image_ark,
                "dgs": data.get("dgsNum"), "records": len(records), "indexed": records}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ full-text search
def _excerpts(text, words, context=160, limit=3):
    low, spans = text.lower(), []
    for w in words:
        for m in re.finditer(re.escape(w.lower()), low):
            spans.append((max(0, m.start() - context), min(len(text), m.end() + context)))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return [re.sub(r"\s+", " ", text[a:b]).strip() for a, b in merged[:limit]]


def fulltext(*, q, place=None, frm=None, to=None, type_=None, collection=None, limit=10, offset=0, full=False):
    """Search the machine-read handwriting of deeds, wills, probate and court records. Results are images (3:1: ARKs)."""
    if not q or not re.fullmatch(r"[^\x00-\x1f]{1,200}", q):
        return failure("invalid-request", problems=[{"field": "q", "issue": "invalid"}])
    params = {"q.text": q, "count": str(max(1, min(int(limit), 50))), "offset": str(max(0, int(offset))), "m.defaultFacets": "on",
              "m.queryRequireDefault": "on"}
    if place:
        params["q.place"] = place
    if frm or to:
        params["q.recordYear.from"], params["q.recordYear.to"] = str(int(frm or 1400)), str(int(to or 2100))
    if collection:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,20}", str(collection)):
            return failure("invalid-request", problems=[{"field": "collection", "issue": "invalid", "expected": "an id from a result, like 2739057 or M9J1-SZ4"}])
        params["c.collectionId"], params["f.collectionId"] = "on", str(collection)
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    if not re.fullmatch(r"[A-Za-z0-9.%+=&_,'-]{1,800}", query):
        return failure("invalid-request", problems=[{"field": "q", "issue": "invalid"}])
    words = [w for w in re.findall(r"[\w']+", q) if len(w) > 1]

    def run():
        hits, data, scanned, start = [], {}, 0, int(offset)
        for _ in range(4 if type_ else 1):                 # a type filter is applied here, so scan a few pages to fill the limit
            page_q = query if not type_ else urllib.parse.urlencode({**params, "count": "50", "offset": str(start)}, quote_via=urllib.parse.quote)
            row, = fetch([{"method": "GET", "path": "/service/search/fulltext/search?" + page_q, "accept": "application/json"}])
            data = _json(row)
            entries = data.get("entries", [])
            scanned += len(entries)
            start += len(entries)
            hits += _collect(entries, type_, words, full)
            if len(hits) >= int(limit) or len(entries) < 50:
                break
        seen_arks = set()
        hits = [h for h in hits if not (h["ark"] in seen_arks or seen_arks.add(h["ark"]))][:int(limit)]
        return _fulltext_result(q, data, offset, hits, type_, scanned)
    return _guard(run)


def _collect(entries, type_, words, full):
    hits = []
    if True:
        for e in entries:
            c = e.get("content") or {}
            kind = c.get("recordType") or ""
            if type_ and type_.lower() not in (kind + " " + str(c.get("title"))).lower():
                continue
            text = c.get("textDocument") or ""
            hit = {"ark": e.get("id"), "collection_id": e.get("collectionId"), "collection": e.get("collectionTitle"), "record_type": kind or None,
                   "place": c.get("recordPlace"), "date": c.get("recordDate") or None, "chars": len(text),
                   "excerpts": _excerpts(text, words), "image_command": f"familysearch image --ark {e.get('id')} --out page.jpg"}
            if full:
                hit["text"] = text
            hits.append(hit)
    return hits


def _fulltext_result(q, data, offset, hits, type_, scanned):
    return {"ok": True, "classification": "familysearch-fulltext", "dispatch_attempted": True, "state": "unchanged", "query": q,
            "total": data.get("results"), "offset": int(offset), "scanned": scanned, "returned": len(hits),
            "note": "Hits are images; the ARK works with `familysearch image --ark`. OCR of handwriting is rough: try spelling variants. Several words WIDEN the search (any of them); put a name in quotes (\"William Regan\") to require the phrase; AND and OR are not operators."
                    + (" --type was applied to the pages scanned, not to the whole result set." if type_ else ""),
            "hits": hits}


# ------------------------------------------------------------------------------------------------ browsing a film
def _film_data(dgs):
    dgs = _pad(dgs)
    row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/film-data", "accept": "application/json", "body": _film_body(str(dgs))}])
    data = _json(row)
    arks = [m.group(0) for u in data.get("images") or [] for m in [re.search(r"3:1:[A-Z0-9-]+", str(u))] if m]
    return data, arks


def _sections(data):
    """The film's contents text split into sections: [{label, years}]. Image ranges are not published for most films."""
    cat = ((data.get("catalogs") or [{}])[0]).get("data") or {}
    dgs = str(data.get("dgsNum"))
    raw = cat.get("film_note") or []
    notes = [n for n in ([raw] if isinstance(raw, dict) else raw) if isinstance(n, dict)]
    mine = next((n for n in notes if str(n.get("digital_film_no")).zfill(9) == dgs.zfill(9)), notes[0] if len(notes) == 1 else None)
    out = []
    for part in str((mine or {}).get("text") or "").split(" -- "):
        m = re.search(r"(\d{4})(?:\s*-\s*(\d{4}))?", part)
        out.append({"label": part.strip(), "years": [int(m.group(1)), int(m.group(2) or m.group(1))] if m else None})
    return out


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
           "nov": 11, "dez": 12, "dec": 12}
_TYPES = {"baptisms": {"Baptism", "Christening", "Birth"}, "births": {"Baptism", "Christening", "Birth"}, "marriages": {"Marriage"},
          "burials": {"Burial", "Death"}, "deaths": {"Burial", "Death"}}


def _date_key(text):
    """(year, month, day) from a date as written ('10. September 1843', '1 Nov 1846', '1846'); missing parts are 0."""
    if not text:
        return None
    y = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", str(text))
    if not y:
        return None
    month = next((n for w in re.findall(r"[A-Za-zÄäÖöÜü]{3,}", str(text)) for k, n in _MONTHS.items() if w.lower().startswith(k)), 0)
    day = re.search(r"\b(\d{1,2})\b\.?", re.sub(r"\b(1[0-9]{3}|20[0-9]{2})\b", "", str(text)))
    return (int(y.group(1)), month, int(day.group(1)) if day and month else 0)


def _flat(key):
    return key[0] * 372 + key[1] * 31 + key[2]


def _target(date, years):
    """(low, high) flat keys of the wanted date or range."""
    if date:
        parts = [int(x) for x in date.split("-")]
        y, m, d = (parts + [0, 0])[:3]
        lo, hi = _flat((y, m, d)), _flat((y, m or 12, d or 31))
        return (lo, hi) if m == 0 or d == 0 else (lo, lo)
    a, _, b = years.partition("-")
    return _flat((int(a), 0, 0)), _flat((int(b or a), 12, 31))


def _probe(arks, number):
    """The indexed records on one image: what each says (type, date, names). One image-data request, paced under the lock."""
    body = {"type": "image-data", "args": {"imageURL": f"https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/{arks[number - 1]}/image.xml",
                                          "locale": "en", "state": {"imageOrFilmUrl": "", "selectedImageIndex": -1, "viewMode": "i"}}}
    row, = fetch([{"method": "POST", "path": "/search/filmdatainfo/image-data", "accept": "application/json", "body": body}])
    records = _json(row).get("records") or []
    events, names = [], []
    for rec in records:
        for p in rec.get("persons", []):
            nm = next((n["nameForms"][0].get("fullText") for n in p.get("names", []) if n.get("nameForms")), None)
            if nm:
                names.append(nm)
            for f in p.get("facts", []):
                kind = str(f.get("type", "")).rsplit("/", 1)[-1]
                key = _date_key((f.get("date") or {}).get("original"))
                if key and (p.get("principal") or not any(q.get("principal") for q in rec.get("persons", []))):
                    events.append({"type": kind, "date": (f.get("date") or {}).get("original"), "key": key, "name": nm})
    return {"image": number, "records": len(records), "events": events, "names": names}


def locate(*, film_dgs, type_=None, date=None, years=None, name=None, probes=16):
    """Which images of a film to look at for a date (and record type, and name), by sampling the indexed records on its images."""
    if not _DGS.match(str(film_dgs)):
        return failure("invalid-request", problems=[{"field": "film", "issue": "invalid", "expected": "digits"}])
    if bool(date) == bool(years) and not name:
        return failure("invalid-request", problems=[{"field": "date", "issue": "invalid", "expected": "exactly one of --date or --years (or --name)"}])
    if type_ and type_ not in _TYPES:
        return failure("invalid-request", problems=[{"field": "type", "issue": "unknown-value", "valid_values": sorted(_TYPES)}])
    try:
        want = _target(date, years) if (date or years) else None
    except ValueError:
        return failure("invalid-request", problems=[{"field": "date", "issue": "invalid", "expected": "YYYY[-MM[-DD]] or --years A-B"}])
    tokens = [t.lower() for t in (name or "").split() if t]

    def run():
        data, arks = _film_data(film_dgs)
        total = len(arks)
        if not total:
            raise LaneError("familysearch-not-found", status=404)
        sections = _sections(data)
        seen, anchors, exact = {}, [], None
        budget = max(1, min(int(probes), 40))

        def take(number):
            nonlocal exact
            if number in seen or not 1 <= number <= total:
                return None
            seen[number] = res = _probe(arks, number)
            kinds = _TYPES.get(type_) if type_ else None
            keys = [e["key"] for e in res["events"] if not kinds or e["type"] in kinds]
            full = [k for k in keys if k[1]]
            keys = full or keys                        # a year-only date (a family's baptism year) says little: prefer exact days
            anchors.append({"image": number, "records": res["records"], "lo": min(map(_flat, keys)) if keys else None,
                            "hi": max(map(_flat, keys)) if keys else None,
                            "dates": sorted({e["date"] for e in res["events"] if (not kinds or e["type"] in kinds)})[:3],
                            "types": sorted({e["type"] for e in res["events"]})})
            if tokens:
                for e in res["events"]:
                    if e["name"] and all(t in e["name"].lower() for t in tokens):
                        exact = {"image": number, "name": e["name"], "type": e["type"], "date": e["date"]}
            return res

        first = max(1, min(budget, 10))
        for i in range(first):
            take(round(total * (i + 0.5) / first))
            if exact:
                break
        used = len(seen)

        def bracket():
            lo_img = hi_img = None
            hit = []
            for a in sorted((a for a in anchors if a["lo"] is not None), key=lambda a: a["image"]):
                if want is None:
                    continue
                if a["hi"] < want[0]:
                    lo_img = a["image"]
                elif a["lo"] > want[1]:
                    hi_img = a["image"]
                    break
                else:
                    hit.append(a["image"])
            return lo_img, hi_img, hit

        while want and not exact and used < budget:
            lo_img, hi_img, hit = bracket()
            a, b = (lo_img or 0), (hi_img or total + 1)
            if b - a <= 2:
                break
            order = sorted((n for n in range(a + 1, b) if n not in seen), key=lambda n: abs(n - (a + b) / 2))
            if not order:
                break
            take(order[0])
            used = len(seen)
        lo_img, hi_img, hit = bracket() if want else (None, None, [])
        if tokens and not exact and want and (lo_img or hi_img):      # a name was asked for: look at the pages in the range
            first_n, last_n = (lo_img or 1), (hi_img or total)
            for n in range(first_n, last_n + 1):
                if exact or len(seen) >= budget:
                    break
                take(n)
        cands = []
        if exact:
            cands.append({"from": exact["image"], "to": exact["image"], "derived": "name", "match": exact})
        elif want:
            cands.append({"from": (lo_img or 1), "to": (hi_img or total), "derived": "anchors-bracket" if lo_img and hi_img else "anchors-open-ended",
                          "between": [lo_img, hi_img], "pages_dated_in_range": hit})
        lo_c, hi_c = (cands[0]["from"], cands[0]["to"]) if cands else (1, total)
        step = max(1, (hi_c - lo_c) // 24)
        return {"ok": True, "classification": "familysearch-locate", "dispatch_attempted": True, "state": "unchanged", "dgs": str(film_dgs),
                "images": total, "sections": sections, "probes": len(seen), "candidates": cands,
                "anchors": [{k: v for k, v in a.items() if k not in ("lo", "hi")} for a in sorted(anchors, key=lambda a: a["image"])],
                "sheet_command": f"familysearch film {film_dgs} --sheet --from {lo_c} --to {hi_c} --step {step} --out sheet.jpg",
                "note": "Anchors come from indexed records on sampled images; a film with no index gives no anchors, and then only the sheet helps."}
    return _guard(run)


def film_sheet(dgs, *, start, end, step, out):
    """A contact sheet of low-resolution thumbnails with their image numbers, to find a page by eye."""
    from PIL import Image, ImageDraw, ImageFont
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir() or not _DGS.match(str(dgs)) or step < 1 or start < 1 or end < start:
        return failure("invalid-request", problems=[{"field": "sheet", "issue": "invalid", "expected": "a new file, --from <= --to, --step >= 1"}])
    numbers = list(range(start, end + 1, step))[:48]

    def run():
        _, arks = _film_data(dgs)
        numbers_ok = [n for n in numbers if n <= len(arks)]
        calls = [{"method": "GET", "path": f"/service/records/storage/dascloud/das/v2/{arks[n - 1]}/thumb_p200.jpg", "accept": "image/jpeg", "binary": True}
                 for n in numbers_ok]
        rows = []
        for i in range(0, len(calls), 12):
            rows += fetch(calls[i:i + 12], pause=TILE_PACE)
        thumbs = [Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB") for r in rows]
        cell_w, cell_h = 200, 260
        cols = 6
        sheet = Image.new("RGB", (cols * cell_w, math.ceil(len(thumbs) / cols) * cell_h), "white")
        draw, font = ImageDraw.Draw(sheet), ImageFont.load_default(size=22)
        for i, (n, t) in enumerate(zip(numbers_ok, thumbs)):
            t.thumbnail((cell_w - 6, cell_h - 36))
            x, y = (i % cols) * cell_w, (i // cols) * cell_h
            sheet.paste(t, (x + 3, y + 30))
            draw.text((x + 6, y + 2), str(n), fill="black", font=font)
        sheet.save(dest, format="PNG" if dest.suffix.lower() == ".png" else "JPEG", quality=88)
        dest.chmod(0o600)
        return {"ok": True, "classification": "familysearch-sheet", "dispatch_attempted": True, "state": "unchanged", "file": str(dest),
                "dgs": str(dgs), "images": numbers_ok, "columns": cols}
    return _guard(run)


def waypoints(*, collection, waypoint=None, query=None, limit=60):
    """Browse a browsable collection's hierarchy (state, county, district...). Leaves list their image ARKs."""
    if not re.fullmatch(r"[0-9]{1,12}", str(collection)) or waypoint and not re.fullmatch(r"[A-Z0-9-]{1,20}:[0-9,]{1,200}", waypoint):
        return failure("invalid-request")
    path = (f"/service/cds/recapi/waypoints/{waypoint}?cc={collection}" if waypoint else f"/service/cds/recapi/collections/{collection}/waypoints")

    def run():
        row, = fetch([{"method": "GET", "path": path, "accept": "application/json"}])
        sds = _json(row).get("sourceDescriptions", [])
        words = (query or "").lower().split()
        kids, leaves = [], []
        for sd in sds:
            title = (sd.get("titles") or [{}])[0].get("value")
            about = str(sd.get("about", ""))
            if sd.get("titleLabel"):
                m = re.search(r"/waypoints/([A-Z0-9-]+:[0-9,]+)\?cc=", about)
                if m and all(w in str(title).lower() for w in words):
                    kids.append({"level": sd["titleLabel"].get("value"), "title": title, "waypoint": m.group(1)})
            elif str(sd.get("resourceType", "")).endswith("DigitalArtifact"):
                a = re.search(r"3:1:[A-Z0-9-]+", about)
                if a:
                    leaves.append(a.group(0))
        return {"ok": True, "classification": "familysearch-waypoints", "dispatch_attempted": True, "state": "unchanged", "collection": str(collection),
                "waypoint": waypoint, "children": kids[:int(limit)], "child_count": len(kids), "images": leaves[:int(limit)], "image_count": len(leaves),
                "next": "familysearch waypoints --collection C --waypoint ID  (images are 3:1: ARKs for `familysearch image --ark`)"}
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
    f.add_argument("--sheet", action="store_true", help="make a contact sheet of thumbnails labelled with image numbers")
    f.add_argument("--from", dest="start", type=int, default=1)
    f.add_argument("--to", dest="end", type=int)
    f.add_argument("--step", type=int, default=1)
    f.add_argument("--out", help="new file for --sheet")
    lo = subs.add_parser("locate", help="which images of a film to look at for a date, type and name (samples the film's indexed records)")
    lo.add_argument("--film", dest="film_dgs", required=True)
    lo.add_argument("--type", dest="type_", choices=sorted(_TYPES))
    lo.add_argument("--date", help="YYYY[-MM[-DD]]")
    lo.add_argument("--years", help="A-B")
    lo.add_argument("--name", help="words that must all appear in a name on the page")
    lo.add_argument("--probes", type=int, default=16, help="most images to inspect (about 3 s each)")
    w = subs.add_parser("waypoints", help="browse a browsable collection's hierarchy (state, county, district) down to image ARKs")
    w.add_argument("--collection", required=True)
    w.add_argument("--waypoint", help="a waypoint id from the previous call, like 9B7J-YWL:1031034401,1031034402")
    w.add_argument("--query", help="words that must appear in a child's title")
    w.add_argument("--limit", type=int, default=60)
    pg = subs.add_parser("page", help="every indexed record on one image (census page neighbors, register entries)")
    pg.add_argument("--ark", help="image ARK 3:1:XXXX")
    pg.add_argument("--film", dest="film_dgs")
    pg.add_argument("--image", dest="number", type=int)
    ft = subs.add_parser("fulltext", help="full-text search of handwritten deeds, wills, probate and court records")
    ft.add_argument("--q", required=True, help="words, as OCR may spell them")
    ft.add_argument("--place", help="e.g. \"Queen Anne's County, Maryland\"")
    ft.add_argument("--from", dest="frm", type=int, help="record year, from")
    ft.add_argument("--to", type=int, help="record year, to")
    ft.add_argument("--type", dest="type_", help="keep hits whose record type contains this (deed, will, probate, court...); applies to the page returned")
    ft.add_argument("--collection", help="a collection id from an earlier result (numeric or like M9J1-SZ4)")
    ft.add_argument("--limit", type=int, default=10)
    ft.add_argument("--offset", type=int, default=0)
    ft.add_argument("--full", action="store_true", help="include each hit's whole page text")
    c = subs.add_parser("catalog", help="catalog by place: record types, then titles, then films with DGS numbers")
    c.add_argument("--place", required=True, help="place name as the catalog writes it, e.g. 'Germany, Bayern, Rockenhausen'")
    c.add_argument("--subject-id", help="a subject id from the first call (a record type such as Church records)")
    c.add_argument("--years", help="1850 or 1850-1900: keep titles whose dates overlap")
    c.add_argument("--limit", type=int, default=10)
    c.add_argument("--films", action="store_true", help="also fetch each title's films (one request each, 3 s apart)")
    c.add_argument("--exact", action="store_true", help="exact place match instead of containing")
    i = subs.add_parser("image", help="save a record image, optionally cropped")
    i.add_argument("--film", dest="film_dgs", help="digital film (DGS) number, with --image")
    i.add_argument("--image", dest="number", type=int, help="image number on the film, starting at 1")
    i.add_argument("--ark", help="image ARK 3:1:XXXX")
    i.add_argument("--das", help="image id TH-...")
    i.add_argument("--record", dest="record_ark", help="a record ARK 1:1:XXXX-XXX: uses the image the record links to")
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
        if args["sheet"]:
            if not (args["out"] and args["end"]):
                result = failure("invalid-request", problems=[{"field": "sheet", "issue": "missing", "expected": "--to and --out"}])
            else:
                result = film_sheet(args["dgs"], start=args["start"], end=args["end"], step=args["step"], out=args["out"])
        else:
            result = film(args["dgs"])
    elif command == "locate":
        result = locate(film_dgs=args["film_dgs"], type_=args["type_"], date=args["date"], years=args["years"], name=args["name"], probes=args["probes"])
    elif command == "waypoints":
        result = waypoints(collection=args["collection"], waypoint=args["waypoint"], query=args["query"], limit=args["limit"])
    elif command == "page":
        result = page_records(ark=args["ark"], film_dgs=args["film_dgs"], number=args["number"])
    elif command == "fulltext":
        result = fulltext(q=args["q"], place=args["place"], frm=args["frm"], to=args["to"], type_=args["type_"], collection=args["collection"],
                          limit=args["limit"], offset=args["offset"], full=args["full"])
    elif command == "catalog":
        result = catalog(place=args["place"], subject_id=args["subject_id"], years=args["years"], limit=args["limit"], films=args["films"],
                         exact=args["exact"])
    else:
        result = image(out=args["out"], film_dgs=args["film_dgs"], number=args["number"], ark=args["ark"], das=args["das"],
                       crop=args["crop"], max_tiles=args["max_tiles"], record_ark=args["record_ark"])
    result = rt.annotate(result)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
