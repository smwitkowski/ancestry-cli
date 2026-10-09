"""Before-state snapshots for edits and removals, and a values-free readback diff.

A snapshot is the person's facts page data (facts, sources, web links, family) saved privately (mode 600) before
any op in SNAPSHOT_OPS is sent. It supplies the inverse for fact-edit and fact-remove, and the data to recreate a
removed person by hand. Diffs report ids and field names only.
"""
from __future__ import annotations

import html
import json
import os
import re
import time

from . import config

SNAPSHOT_OPS = frozenset(("fact-edit", "fact-remove", "person-remove", "weblink-remove", "citation-remove",
                          "media-remove", "fact-detach-source", "fact-attach-source", "note-set"))
_FACT_KEYS = ("AssertionId", "Type", "TypeString", "Title", "Date", "Place", "PlaceGpids", "IsAlternate", "HasCustomTitle",
              "Description", "SourceCitationIDs")
_FIELDS = ("TypeString", "Date", "Place", "PlaceGpids", "IsAlternate", "Description", "SourceCitationIDs")
_GPID_ORDER = ("CityId", "TownshipId", "CountyId", "StateId", "CountryId")      # most specific first


class SnapshotError(ValueError):
    pass


def person_data(page_text):
    m = re.search(r'<script[^>]*id="person-data"[^>]*>(.*?)</script>', page_text, re.DOTALL)
    if not m:
        raise SnapshotError("person-data-missing")
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except ValueError:
        return json.loads(html.unescape(raw))


def snapshot_from_page(page_text, tree_id, person_id):
    pr = person_data(page_text)["person"]["PersonResearch"]
    return {"tree_id": tree_id, "person_id": person_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "name": pr.get("PersonFullName"), "living": pr.get("IsPersonLiving"),
            "facts": [{k: f.get(k) for k in _FACT_KEYS} | {"Value": f.get("Value")} for f in pr.get("PersonFacts", [])],
            "sources": [{**{k: s.get(k) for k in ("CitationId", "AssertionIds", "Title", "Detail", "RecordId", "ViewRecordUrl")}, "custom": custom, "SourceId": s.get("SourceId") or None}
                        for custom, key in ((False, "PersonSources"), (True, "UGCPersonSources")) for s in (pr.get(key) or [])],
            "weblinks": pr.get("PersonWebLinks"), "family": pr.get("PersonFamily")}


def save(snapshot, op):
    d = config.snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{snapshot['ts'].replace(':', '')}-{time.monotonic_ns() % 10**6:06d}-{op}-{snapshot['person_id']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(snapshot, handle, ensure_ascii=True)
    return str(path)


def _facts(snapshot):
    return {str(f["AssertionId"]): f for f in snapshot["facts"] if f.get("AssertionId")}


def diff(before, after):
    """Ids and changed field names only, never values."""
    b, a = _facts(before), _facts(after)
    return {"added": sorted(set(a) - set(b)), "removed": sorted(set(b) - set(a)),
            "changed": {i: [k for k in _FIELDS if b[i].get(k) != a[i].get(k)] for i in set(a) & set(b)
                        if any(b[i].get(k) != a[i].get(k) for k in _FIELDS)},
            "name_changed": before.get("name") != after.get("name")}


def gpid(place_gpids):
    """The place id the site stores for a fact: the most specific non-zero id in PlaceGpids (verified against LifeEvents)."""
    if isinstance(place_gpids, dict):
        for key in _GPID_ORDER:
            if place_gpids.get(key):
                return str(place_gpids[key])
    return ""


def restore_fields(snapshot, assertion_id):
    """Fact fields that put one fact back as it was (used as the inverse of fact-edit / fact-remove)."""
    f = _facts(snapshot).get(str(assertion_id))
    if not f or not f.get("TypeString"):
        return None
    gender = next((x.get("Value") for x in snapshot["facts"] if x.get("Type") == 44), "") or ""
    out = {"eventType": f["TypeString"], "date": f.get("Date") or "", "description": f.get("Description") or "",
           "gender": gender if isinstance(gender, str) else ""}
    if f.get("TypeString") == "CustomEvent" and f.get("Title"):
        out["label"] = f["Title"]                   # a custom event's own label is its Title
    if f.get("Place"):
        out["location"] = {"placeName": f["Place"], "GPID": gpid(f.get("PlaceGpids")), "showUnderline": False}
    return out
