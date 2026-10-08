"""Score search hits against a person's facts (read-only): which records are likely the same person.

`ancestry match --surname S [--given G --birth Y --location L] [--collection C] [--limit N]` runs the search, then asks Ancestry's
match explainer how well each of the first N hits agrees with the search terms (exact / similar / different field counts)."""
from __future__ import annotations

import json
import time

from . import reads
from . import runtime as rt
from .runtime import failure

_URL = "https://www.ancestry.com/api/search-results/record/{c}/{r}/?client=searchui-resultsui&viewType=Hover" \
       "&pageName=ancestry%20us%20%3A%20search%20%3A%20results&hasOremCorrections=UNKNOWN"


def tag(counts):
    """A plain verdict from the explainer's counts (the thresholds are a heuristic, not a fact about the record)."""
    e, s, d = (counts.get(k) or 0 for k in ("exact", "similar", "different"))
    if e == s == d == 0:
        return "attached"            # the explainer returns all zeros for records Ancestry already links to the person
    if e >= 3 and d <= 1:
        return "likely_match"
    if e >= 1 and d >= 2:
        return "conflict"
    if e == 0 and s > 0:
        return "namesake"
    if 1 <= e <= 2 and d <= 1:
        return "maybe"
    return "weak"


def _name(item):
    return next((f.get("text") for f in item.get("fields", []) if f.get("label") == "Name"), None)


def match(*, surname, given=None, birth=None, death=None, location=None, collection=None, limit=5, bridge=None, spouse=None):
    if not surname:
        return failure("missing-arguments")
    terms = dict(given=given, surname=surname, birth=birth, death=death, location=location, full=True, bridge=bridge, spouse=spouse)
    found = reads.read(action="search", **terms)                 # the all-records search carries requestBase64
    if not found.get("ok"):
        return found
    results = (found.get("body") or {}).get("results") or {}
    request = results.get("requestBase64")
    if collection:                                               # the same terms, restricted to one collection's hits
        inside = reads.read(action="search", collection=collection, **terms)
        if not inside.get("ok"):
            return inside
        results = {**results, "items": ((inside.get("body") or {}).get("results") or {}).get("items"),
                   "hitCount": ((inside.get("body") or {}).get("results") or {}).get("hitCount")}
    items = [i for i in (results.get("items") or []) if i.get("recordId") and i.get("collectionId")][:max(1, min(limit, 20))]
    if not request:
        return failure("native-operation-failed")
    bridge = bridge or rt.load_bridge()
    rows = []
    try:
        with rt.quiet(), rt.lock("ancestry"):
            lease = rt.open_lane(bridge)
            inner = bridge.ChromeBrowserSession(cdp_url=lease.base, target_id=lease.target_id)
            for item in items:
                lease.check()
                resp = inner.post_read(_URL.format(c=item["collectionId"], r=item["recordId"]), json={"base64Request": request})
                row = {"collection_id": str(item["collectionId"]), "record_id": str(item["recordId"]),
                       "collection": item.get("collectionTitle"), "name": _name(item)}
                if 200 <= getattr(resp, "status_code", 0) < 300:
                    data = json.loads(resp.text)
                    counts = data.get("matchCounts") or {}
                    row.update(tag=tag(counts), match_counts=counts, explanations=(data.get("explanations") or [])[:3])
                else:
                    row.update(tag="unscored", status=resp.status_code)
                rows.append(row)
                time.sleep(0.15)
    except rt.LaneError as exc:
        return failure(exc.code, **exc.detail)
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")
    order = {"likely_match": 0, "maybe": 1, "conflict": 2, "namesake": 3, "attached": 4, "weak": 5, "unscored": 6}
    rows.sort(key=lambda r: order.get(r["tag"], 9))
    return {"ok": True, "classification": "match", "dispatch_attempted": True, "state": "unchanged",
            "scored": len(rows), "hit_count": results.get("hitCount"), "candidates": rows,
            "note": "Tags are a heuristic on the explainer's counts; read the record before attaching or recording anything."}
