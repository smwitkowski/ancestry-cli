"""`newspapers`: free newspaper archives over plain HTTP (read-only). One command family, several providers.

  loc   Chronicling America at the Library of Congress (loc.gov JSON API), 1770-1963
  pa    Pennsylvania Newspaper Archive (panewsarchive.psu.edu, an Open ONI / chronam site)

nys (nyshistoricnewspapers.org) and brooklyn (bklyn.newspapers.com) sit behind a bot check that plain HTTP cannot pass. The tool does not
try to pass it: they are listed by `doctor` and reported as skipped. Requests identify the tool, are spaced 1.5 s apart under a lock, and
back off on 429. IDs look like `loc:sn87075008/1935-02-14/ed-1/seq-7` (provider : lccn / date / edition / page).

  newspapers doctor
  newspapers search --q TEXT [--provider loc|pa|all] [--from Y[-M-D]] [--to Y[-M-D]] [--state NAME] [--paper LCCN] [--limit N]
  newspapers page ID [--find WORDS] [--context N] [--max-chars N]
  newspapers image ID --out FILE [--crop x,y,w,h] [--scale PCT]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import runtime as rt
from .runtime import LaneError, failure

UA = "ancestry-cli newspapers (personal genealogy research; read-only; https://github.com/smwitkowski/ancestry-cli)"
PACE = 1.5
PROVIDERS = {"loc": "https://www.loc.gov", "pa": "https://panewsarchive.psu.edu"}
BLOCKED = {"nys": "https://www.nyshistoricnewspapers.org", "brooklyn": "https://bklyn.newspapers.com"}
_ID = re.compile(r"(loc|pa):(sn[0-9]{6,10}|[0-9]{8,10})/(\d{4}-\d{2}-\d{2})/ed-(\d{1,2})/seq-(\d{1,4})\Z")


def lock_file():
    override = os.environ.get("NEWSPAPERS_CLI_LOCK_FILE")
    return Path(override) if override else Path(tempfile.gettempdir()) / f"newspapers-cli-locks-{os.getuid()}" / "http.lock"


def _get(url, *, accept="*/*", binary=False, retries=1):
    """One paced GET. Raises LaneError(<classification>, provider=...) for bot checks, rate limits and missing pages."""
    host = urllib.parse.urlsplit(url).netloc
    if not any(url.startswith(base + "/") or url.startswith(base + "?") for base in list(PROVIDERS.values()) + ["https://tile.loc.gov"]):
        raise ValueError("host-not-allowed")
    for attempt in range(retries + 1):
        with rt.quiet(), rt.lock("newspapers", path=lock_file(), interval=PACE):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body = resp.read(40_000_000)
                    status, ctype = resp.status, resp.headers.get("content-type", "")
            except urllib.error.HTTPError as exc:
                status, ctype, body = exc.code, exc.headers.get("content-type", ""), exc.read(200_000)
                wait = exc.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < retries:
                    time.sleep(3)
                    continue
                raise LaneError("newspapers-unavailable", provider=host) from None
        if status == 429 or (status == 503 and b"Just a moment" not in body[:3000]):
            if attempt < retries:
                time.sleep(min(60, int(wait) if wait and wait.isdigit() else 20))
                continue
            raise LaneError("newspapers-rate-limited", provider=host, status=status)
        if status in (403, 503) or b"Just a moment" in body[:3000]:
            raise LaneError("newspapers-check-required", provider=host, status=status)
        if status == 404:
            raise LaneError("newspapers-not-found", provider=host, status=404)
        if not 200 <= status < 300:
            raise LaneError("newspapers-unavailable", provider=host, status=status)
        return body if binary else body.decode("utf-8", "replace"), ctype
    raise LaneError("newspapers-unavailable", provider=host)


def _json(url):
    text, _ = _get(url, accept="application/json")
    try:
        return json.loads(text)
    except ValueError:
        raise LaneError("newspapers-unavailable", provider=urllib.parse.urlsplit(url).netloc) from None


def _guard(fn):
    try:
        return fn()
    except LaneError as exc:
        return failure(exc.code, **exc.detail)
    except ValueError:
        return failure("invalid-request")
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")


def _year_or_date(value, end):
    if not value:
        return None
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", value)
    if not m:
        raise ValueError("date")
    y, mo, d = m.group(1), m.group(2) or ("12" if end else "01"), m.group(3) or ("31" if end else "01")
    return f"{y}-{mo}-{d}"


def _hit_id(provider, url_or_path):
    m = re.search(r"(sn\d{6,10}|\b\d{8,10}\b)/(\d{4}-\d{2}-\d{2})/ed-(\d+)/(?:\?sp=|seq-)(\d+)", url_or_path)
    return f"{provider}:{m.group(1)}/{m.group(2)}/ed-{m.group(3)}/seq-{m.group(4)}" if m else None


def _snippet(text, words, width=220):
    low = (text or "").lower()
    for w in words:
        i = low.find(w.lower())
        if i >= 0:
            return re.sub(r"\s+", " ", text[max(0, i - width // 2):i + width // 2]).strip()
    return None


# ------------------------------------------------------------------------------------------------ search
def _search_loc(q, frm, to, state, paper, limit, page):
    params = {"q": q, "fo": "json", "c": str(limit), "at": "results,pagination", "sp": str(page), "dl": "page"}
    if frm or to:
        params["dates"] = f"{frm or '1770-01-01'}/{to or '1963-12-31'}"
    facets = []
    if state:
        facets.append("location_state:" + state.lower())
    if paper:
        facets.append("number_lccn:" + paper.lower())
    if facets:
        params["fa"] = "|".join(facets)
    data = _json("https://www.loc.gov/collections/chronicling-america/?" + urllib.parse.urlencode(params))
    words = re.findall(r"[\w']+", q)
    hits = []
    for r in data.get("results", []):
        pid = _hit_id("loc", str(r.get("id", "")))
        if not pid:
            continue
        hits.append({"id": pid, "provider": "loc", "title": re.sub(r"^Image \d+ of ", "", r.get("title", "")), "date": r.get("date"),
                     "state": (r.get("location_state") or [None])[0], "url": r.get("url", "").split("&q=")[0].split("?")[0] + "?sp=" + str(r.get("shelf_id") or ""),
                     "snippet": _snippet(" ".join(r.get("description") or []), words)})
    return {"total": (data.get("pagination") or {}).get("total"), "hits": hits}


def _search_pa(q, frm, to, state, paper, limit, page):
    params = {"proxtext": q, "rows": str(limit), "page": str(page), "format": "json", "sort": "relevance"}
    if frm or to:
        params["date1"], params["date2"] = frm or "1736-01-01", to or "2100-12-31"     # ISO dates; plain years are ignored by this site
    if paper:
        params["lccn"] = paper
    if state:
        params["state"] = state
    data = _json("https://panewsarchive.psu.edu/search/pages/results/?" + urllib.parse.urlencode(params))
    words = re.findall(r"[\w']+", q)
    hits = []
    for it in data.get("items", []):
        pid = _hit_id("pa", str(it.get("id", "")).strip("/").replace("lccn/", "") + "/")
        pid = pid or (f"pa:{it['lccn']}/{it['date'][:4]}-{it['date'][4:6]}-{it['date'][6:8]}/ed-{it.get('edition') or 1}/seq-{it['sequence']}" if it.get("date") else None)
        if not pid:
            continue
        d = it.get("date", "")
        hits.append({"id": pid, "provider": "pa", "title": it.get("title"), "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                     "state": (it.get("state") or [None])[0] if isinstance(it.get("state"), list) else (it.get("state") or None), "url": "https://panewsarchive.psu.edu" + str(it.get("id")),
                     "snippet": _snippet(it.get("ocr_eng"), words)})
    return {"total": data.get("totalItems"), "hits": hits}


_SEARCH = {"loc": _search_loc, "pa": _search_pa}


def search(*, q, provider="all", frm=None, to=None, state=None, paper=None, limit=10, page=1):
    if not q or provider not in ("all", *PROVIDERS):
        return failure("invalid-request", problems=[{"field": "provider" if q else "q", "issue": "invalid"}])

    def run():
        f, t = _year_or_date(frm, False), _year_or_date(to, True)
        chosen = list(PROVIDERS) if provider == "all" else [provider]
        out, totals, skipped = [], {}, [{"provider": p, "reason": "bot check: plain HTTP cannot pass it"} for p in BLOCKED] if provider == "all" else []
        for name in chosen:
            try:
                res = _SEARCH[name](q, f, t, state, paper, max(1, min(int(limit), 50)), max(1, int(page)))
            except LaneError as exc:
                if provider != "all":
                    raise
                skipped.append({"provider": name, "reason": exc.code})
                continue
            totals[name] = res["total"]
            out += res["hits"]
        return {"ok": True, "classification": "newspapers-search", "dispatch_attempted": True, "state": "unchanged", "query": q,
                "totals": totals, "returned": len(out), "hits": out, "skipped": skipped}
    return _guard(run)


# ------------------------------------------------------------------------------------------------ pages
def _parse_id(pid):
    m = _ID.match(str(pid))
    if not m:
        raise ValueError("id")
    return m.group(1), m.group(2), m.group(3), int(m.group(4)), int(m.group(5))


def _loc_resource(lccn, date, ed, seq):
    return _json(f"https://www.loc.gov/resource/{lccn}/{date}/ed-{ed}/?sp={seq}&fo=json")


def page(pid, *, find=None, context=300, max_chars=20000):
    prov, lccn, date, ed, seq = _parse_id(pid)

    def run():
        if prov == "loc":
            res = _loc_resource(lccn, date, ed, seq)
            ft = (res.get("resource") or {}).get("fulltext_file") or res.get("fulltext_service")
            if not ft or not ft.startswith("https://tile.loc.gov/"):
                raise LaneError("newspapers-not-found", provider="loc", status=404)
            blob = _json(ft)
            text = next((v.get("full_text") for v in blob.values() if isinstance(v, dict) and v.get("full_text")), "")
            item = res.get("item") or {}
            title = re.sub(r"^Image \d+ of ", "", str(item.get("title") or res.get("title") or ""))
            url = f"https://www.loc.gov/resource/{lccn}/{date}/ed-{ed}/?sp={seq}"
        else:
            text, _ = _get(f"https://panewsarchive.psu.edu/lccn/{lccn}/{date}/ed-{ed}/seq-{seq}/ocr.txt", accept="text/plain")
            title = None
            url = f"https://panewsarchive.psu.edu/lccn/{lccn}/{date}/ed-{ed}/seq-{seq}/"
        out = {"ok": True, "classification": "newspapers-page", "dispatch_attempted": True, "state": "unchanged", "id": pid, "provider": prov,
               "lccn": lccn, "date": date, "edition": ed, "page": seq, "title": title, "url": url, "chars": len(text)}
        if find:
            words = [w for w in re.findall(r"[\w']+", find) if len(w) > 1]
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
            out["excerpts"] = [re.sub(r"[ \t]+", " ", text[a:b]).strip() for a, b in merged[:6]]
            out["matches"] = len(spans)
        else:
            out["text"] = text[:max_chars]
            out["truncated"] = len(text) > max_chars
        return out
    return _guard(run)


def _crop_box(spec, width, height):
    x, y, w, h = (float(v) for v in spec.split(","))
    if max(x, y, w, h) <= 1:
        x, y, w, h = x * width, y * height, w * width, h * height
    if not (0 <= x < width and 0 <= y < height and w > 0 and h > 0):
        raise ValueError("crop")
    return int(x), int(y), int(min(w, width - x)), int(min(h, height - y))


def image(pid, *, out, crop=None, scale=None):
    from PIL import Image
    prov, lccn, date, ed, seq = _parse_id(pid)
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir():
        return failure("invalid-request", problems=[{"field": "out", "issue": "invalid", "expected": "a new file in an existing folder"}])
    if crop and len(crop.split(",")) != 4:
        return failure("invalid-request", problems=[{"field": "crop", "issue": "invalid", "expected": "x,y,w,h"}])

    def run():
        pct = int(scale) if scale else (100 if crop else 30)
        if prov == "loc":
            res = _loc_resource(lccn, date, ed, seq)
            files = [f for f in (res.get("page") or []) if f.get("mimetype") == "image/jp2" and f.get("info")]
            if not files:
                raise LaneError("newspapers-not-found", provider="loc", status=404)
            base = files[0]["info"].rsplit("/info.json", 1)[0]
            width, height = int(files[0]["width"]), int(files[0]["height"])
            region = "full" if not crop else "{},{},{},{}".format(*_crop_box(crop, width, height))
            data, _ = _get(f"{base}/{region}/pct:{pct}/0/default.jpg", accept="image/jpeg", binary=True)
            img = Image.open(io.BytesIO(data)).convert("RGB")
        else:
            data, _ = _get(f"https://panewsarchive.psu.edu/lccn/{lccn}/{date}/ed-{ed}/seq-{seq}.jp2", binary=True)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            width, height = img.size
            if crop:
                x, y, w, h = _crop_box(crop, width, height)
                img = img.crop((x, y, x + w, y + h))
            if pct != 100:
                img = img.resize((max(1, img.width * pct // 100), max(1, img.height * pct // 100)))
        img.save(dest, format="PNG" if dest.suffix.lower() == ".png" else "JPEG", quality=92)
        dest.chmod(0o600)
        return {"ok": True, "classification": "newspapers-image", "dispatch_attempted": True, "state": "unchanged", "id": pid, "file": str(dest),
                "full_size": [width, height], "saved_size": list(img.size), "scale_percent": pct}
    return _guard(run)


def doctor():
    out = {"ok": True, "classification": "doctor", "lock_file": str(lock_file()), "providers": {}}
    probes = {"loc": "https://www.loc.gov/collections/chronicling-america/?q=ringgold&fo=json&c=1&at=pagination",
              "pa": "https://panewsarchive.psu.edu/search/pages/results/?proxtext=ringgold&rows=1&format=json"}
    for name, url in probes.items():
        try:
            _get(url, accept="application/json", retries=0)
            out["providers"][name] = "ok"
        except LaneError as exc:
            out["providers"][name] = exc.code
            out["ok"] = False
    for name in BLOCKED:
        out["providers"][name] = "bot-check (not supported over plain HTTP)"
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="newspapers", description="Free newspaper archives over plain HTTP (read-only).")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="check each provider")
    s = subs.add_parser("search", help="full-text search of newspaper pages")
    s.add_argument("--q", required=True, help="words or a \"quoted phrase\"")
    s.add_argument("--provider", default="all", choices=("all", *PROVIDERS))
    s.add_argument("--from", dest="frm", help="YYYY or YYYY-MM-DD")
    s.add_argument("--to", help="YYYY or YYYY-MM-DD")
    s.add_argument("--state", help="e.g. Ohio")
    s.add_argument("--paper", help="a newspaper's LCCN, e.g. sn87075008")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--page", type=int, default=1)
    g = subs.add_parser("page", help="OCR text of one page (or excerpts around words)")
    g.add_argument("id")
    g.add_argument("--find", help="return excerpts around these words instead of the whole page")
    g.add_argument("--context", type=int, default=300, help="characters of context each side of a match")
    g.add_argument("--max-chars", type=int, default=20000)
    i = subs.add_parser("image", help="save a page image, optionally cropped")
    i.add_argument("id")
    i.add_argument("--out", required=True)
    i.add_argument("--crop", help="x,y,w,h in full-size pixels, or fractions when all are 1 or less")
    i.add_argument("--scale", type=int, help="percent of full size (default 30 for a whole page, 100 for a crop)")
    return p


def main(argv=None):
    try:
        args = vars(build_parser().parse_args(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        print(json.dumps(rt.annotate(failure("usage-error"))))
        return 2
    command = args.pop("command")
    try:
        if command == "doctor":
            result = doctor()
        elif command == "search":
            result = search(q=args["q"], provider=args["provider"], frm=args["frm"], to=args["to"], state=args["state"], paper=args["paper"],
                            limit=args["limit"], page=args["page"])
        elif command == "page":
            result = page(args["id"], find=args["find"], context=args["context"], max_chars=args["max_chars"])
        else:
            result = image(args["id"], out=args["out"], crop=args["crop"], scale=args["scale"])
    except ValueError:
        result = failure("invalid-request", problems=[{"field": "id", "issue": "invalid", "expected": "provider:lccn/YYYY-MM-DD/ed-N/seq-N"}])
    result = rt.annotate(result)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
