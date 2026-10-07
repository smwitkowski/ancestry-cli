"""Whole-page census (and any indexed image) export, read-only: everyone indexed on one image, grouped by household.

`ancestry census --collection C (--image I | --record R) [--surname S] [--out FILE.csv]`
Uses the image viewer's index panel, which lists every indexed person on the image with all the collection's columns."""
from __future__ import annotations

import csv
from pathlib import Path

from . import reads
from .runtime import failure


def _rows(panel):
    labels = {f["fieldName"]: f.get("labelText") or f["fieldName"] for f in panel.get("fieldLabels", [])}
    out = []
    for rec in panel.get("records", []):
        fields = {labels.get(f["fieldName"], f["fieldName"]): (f.get("correctedValue") or f.get("value") or "")
                  for f in rec.get("recordFields", [])}
        out.append({"person_id": str(rec.get("pid")), "household_id": str(rec.get("householdId") or ""), "name": rec.get("fullName"),
                    "fields": {k: v for k, v in fields.items() if v}, "_all": fields})
    return out, list(labels.values())


def census(*, collection, image_id=None, record_id=None, surname=None, out=None, bridge=None):
    if not collection or not (image_id or record_id):
        return failure("missing-arguments")
    dest = Path(out) if out else None
    if dest and (dest.exists() or not dest.parent.is_dir()):
        return failure("configuration-error", problems=[{"field": "out", "issue": "invalid", "expected": "a new file in an existing folder"}])
    target_pid = None
    if not image_id:
        card = reads.read(action="record", collection=collection, record_id=record_id, full=True, bridge=bridge)
        body = card.get("body") if card.get("ok") else None
        image_id = (body or {}).get("imageId") or next(iter((body or {}).get("imageIds") or []), None)
        if not image_id:
            return failure("image-unavailable")
        target_pid = str(record_id)
    res = reads.read(action="get", name="image_index_panel", params={"dbId": str(collection), "imageId": str(image_id)}, full=True, bridge=bridge)
    if not res.get("ok"):
        return res
    rows, labels = _rows(res["body"])
    if not rows:
        return {"ok": True, "classification": "census", "dispatch_attempted": True, "state": "unchanged", "image_id": str(image_id),
                "people_on_image": 0, "households": [], "note": "No indexed people on that image."}
    words = (surname or "").lower().split()
    keep = {r["household_id"] for r in rows if words and all(w in (r["name"] or "").lower() for w in words)} if words else None
    households, order = {}, []
    for r in rows:
        if keep is not None and r["household_id"] not in keep:
            continue
        key = r["household_id"] or r["person_id"]
        if key not in households:
            households[key] = []
            order.append(key)
        households[key].append({"person_id": r["person_id"], "name": r["name"], "target": r["person_id"] == target_pid or None,
                                "match": bool(words and all(w in (r["name"] or "").lower() for w in words)) or None, **r["fields"]})
    if dest:
        with dest.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["household_id", "person_id", "name"] + labels)
            for r in rows:
                if keep is None or r["household_id"] in keep:
                    writer.writerow([r["household_id"], r["person_id"], r["name"]] + [r["_all"].get(label, "") for label in labels])
        dest.chmod(0o600)
    return {"ok": True, "classification": "census", "dispatch_attempted": True, "state": "unchanged", "collection": str(collection),
            "image_id": str(image_id), "people_on_image": len(rows), "households_on_image": len({r["household_id"] for r in rows}),
            "households": [{"household_id": k, "members": households[k]} for k in order], "file": str(dest) if dest else None,
            "note": "Neighbors are the households listed before and after yours on this page; a household at the page edge continues on the adjacent image."}
