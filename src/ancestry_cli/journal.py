"""Append-only journal of tree writes, so mapping-mode creations can be listed and undone.

Stores ids and the inverse operation only (no descriptions, dates or other values).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config


def _rows():
    if not config.journal_path().exists():
        return []
    return [json.loads(line) for line in config.journal_path().read_text().splitlines() if line.strip()]


def _append(row):
    config.journal_path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.journal_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def inverse(op, tree_id, person_id, fields, ids, snapshot=None):
    """The write that undoes this one, or None. fact-edit / fact-remove restore from the before-snapshot."""
    if op == "note-set" and snapshot:
        try:
            prior = json.loads(Path(snapshot).read_text()).get("note_before")
        except Exception:
            prior = None
        if isinstance(prior, str):
            return {"op": "note-set", "person_id": person_id, "fields": {"text": prior}}
    if op in ("fact-edit", "fact-remove") and snapshot and fields.get("assertion_id"):
        try:
            from .snapshots import restore_fields
            prior = restore_fields(json.loads(Path(snapshot).read_text()), fields["assertion_id"])
        except Exception:
            prior = None
        if prior:
            if op == "fact-edit":
                return {"op": "fact-edit", "person_id": person_id, "assertion_id": str(fields["assertion_id"]), "fields": prior}
            return {"op": "fact-add", "person_id": person_id, "fields": prior}
    if op == "relative-add" and ids.get("newPid"):
        # no name is stored: person-remove reads the display name from the page when it runs
        return {"op": "person-remove", "person_id": int(ids["newPid"]), "fields": {}}
    if op == "fact-add" and ids.get("AttributeIds"):
        return {"op": "fact-remove", "person_id": person_id, "assertion_id": ids["AttributeIds"][0], "fields": {}}
    if op == "fact-attach-source" and fields.get("citation_id"):
        return {"op": "fact-detach-source", "person_id": person_id, "assertion_id": fields["assertion_id"],
                "fields": {"citation_id": fields["citation_id"]}}
    if op in ("hint-maybe", "hint-no") and fields.get("hint_id"):
        return {"op": "hint-new", "person_id": person_id, "fields": {"hint_id": fields["hint_id"]}}
    if op == "hint-ignore" and fields.get("hint_id"):
        return {"op": "hint-restore", "person_id": person_id, "fields": {"hint_id": fields["hint_id"]}}
    if op == "tag-add" and ids.get("addedTags"):
        return {"op": "tag-remove", "person_id": person_id, "fields": {"tags": ids["addedTags"]}}
    if op == "tag-remove" and ids.get("removedTags"):
        return {"op": "tag-add", "person_id": person_id, "fields": {"tags": ids["removedTags"]}}
    if op == "source-create" and ids.get("gid"):
        return {"op": "source-delete", "person_id": person_id, "fields": {"source_id": ids["gid"]}}
    if op == "media-upload" and ids.get("mediaId"):
        return {"op": "media-remove", "person_id": person_id, "fields": {"media_id": ids["mediaId"]}}
    if op == "weblink-add" and ids.get("webLinkId"):
        return {"op": "weblink-remove", "person_id": person_id, "fields": {"web_link_id": ids["webLinkId"]}}
    return None


def request_hash(op, tree_id, person_id, fields):
    """Stable fingerprint of a write request, used to refuse accidental repeats."""
    import hashlib
    clean = {k: v for k, v in sorted(fields.items()) if k not in ("confirm_tree", "force")}
    return hashlib.sha256(json.dumps([op, tree_id, person_id, clean], sort_keys=True, default=str).encode()).hexdigest()[:32]


def find_duplicate(req_hash, hours=24):
    """The journal id of a live, unreversed, recent write with this fingerprint (outcome ok or unknown), else None."""
    rows = _rows()
    done = {r["undo_of"] for r in rows if "undo_of" in r} | {r["resolved"] for r in rows if "resolved" in r and r.get("as") == "failed"}
    cutoff = time.time() - hours * 3600
    for r in reversed(rows):
        if r.get("req_hash") == req_hash and r.get("outcome") in ("ok", "unknown") and r["id"] not in done:
            try:
                ts = time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                return r["id"]
    return None


def _reconcile(op, tree_id, person_id, fields, ids=None):
    """A successful removal resolves the creation it reverses, however it was done (CLI undo or by hand)."""
    match = {
        # removing a person resolves its creation and every later write journaled against it
        "person-remove": lambda r: (r["op"] == "relative-add" and str(r["ids"].get("newPid")) == str(person_id))
        or (str(r.get("person_id")) == str(person_id) and r["op"] != "person-remove"),
        "fact-remove": lambda r: r["op"] == "fact-add" and str(fields.get("assertion_id")) in map(str, r["ids"].get("AttributeIds", [])),
        "weblink-remove": lambda r: r["op"] == "weblink-add" and r["ids"].get("webLinkId") == fields.get("web_link_id"),
        "media-remove": lambda r: r["op"] == "media-upload" and r["ids"].get("mediaId") == fields.get("media_id"),
        "fact-detach-source": lambda r: r["op"] == "fact-attach-source" and r.get("person_id") == person_id
        and str(r.get("assertion_id")) == str(fields.get("assertion_id"))
        and str(r.get("citation_id")) == str(fields.get("citation_id")),
        "citation-remove": lambda r: r["op"] == "citation-add" and str(r["ids"].get("gid")) == str(fields.get("citation_id")),
        "tag-remove": lambda r: r["op"] == "tag-add" and r.get("person_id") == person_id and sorted(map(str, r["ids"].get("addedTags", []))) == sorted(map(str, (ids or {}).get("removedTags", []))),
        "hint-new": lambda r: r["op"] in ("hint-maybe", "hint-no") and str(r["ids"].get("hintId")) == str(fields.get("hint_id")),
        "hint-restore": lambda r: r["op"] == "hint-ignore" and str(r["ids"].get("hintId")) == str(fields.get("hint_id")),
    }.get(op)
    if not match:
        return 0
    done = {r["undo_of"] for r in _rows() if "undo_of" in r} | {r["resolved"] for r in _rows() if "resolved" in r}
    n = 0
    for r in _rows():
        if "undo_of" not in r and "resolved" not in r and r["id"] not in done and r.get("tree_id") == tree_id and match(r):
            _append({"undo_of": r["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": op})
            n += 1
    return n


def record(op, tree_id, person_id, fields, ids, outcome="ok", snapshot=None, req_hash=None):
    """outcome: ok | failed | unknown. Unknown means a request left but its result was not confirmed."""
    undo = inverse(op, tree_id, person_id, fields, ids, snapshot) if outcome == "ok" else None
    row_id = len(_rows())
    _append({"id": row_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "op": op,
             "tree_id": tree_id, "person_id": person_id, "assertion_id": fields.get("assertion_id"),
             "citation_id": fields.get("citation_id"), **({"hint_id": str(fields["hint_id"])} if fields.get("hint_id") else {}),
             "ids": ids, "undo": undo, "undone": False, "outcome": outcome,
             **({"snapshot": snapshot} if snapshot else {}), **({"req_hash": req_hash} if req_hash else {})})
    if outcome == "ok" and _reconcile(op, tree_id, person_id, fields, ids):
        # the removal cancelled one of our own creations: the pair is settled, so the removal needs no undo either
        _append({"undo_of": row_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": "paired"})
    return row_id


def manual():
    """Creations with no known undo route (sources, citations): delete these in the Ancestry UI."""
    rows = _rows()
    done = {r["undo_of"] for r in rows if "undo_of" in r} | {r["resolved"] for r in rows if "resolved" in r}
    return [r for r in rows if "undo_of" not in r and "resolved" not in r and r["id"] not in done and not r.get("undo")
            and (r.get("outcome") == "unknown" or (r.get("outcome", "ok") == "ok"
                 and r["op"] in ("source-create", "citation-add", "hint-accept", "hint-parent-accept")))]


def outstanding():
    """Creations that still have an inverse and have not been undone."""
    done = {r["undo_of"] for r in _rows() if "undo_of" in r} | {r["resolved"] for r in _rows() if "resolved" in r}
    return [r for r in _rows() if "undo_of" not in r and r.get("undo") and r["id"] not in done]


def summary(*, since=None, names=False, tree_id=None):
    """What the journal recorded since a time (ISO date or datetime, UTC), grouped by person and op. names=True looks each person up."""
    rows = [r for r in _rows() if r.get("op") and (not since or r["ts"] >= since) and (tree_id is None or str(r["tree_id"]) == str(tree_id))]
    by_op, people, new_people = {}, {}, []
    for r in rows:
        by_op[r["op"]] = by_op.get(r["op"], 0) + 1
        entry = people.setdefault((str(r["tree_id"]), str(r["person_id"])), {"tree_id": str(r["tree_id"]), "person_id": str(r["person_id"]), "ops": {}, "journal_ids": []})
        entry["ops"][r["op"]] = entry["ops"].get(r["op"], 0) + 1
        entry["journal_ids"].append(r["id"])
        if r["op"] == "relative-add" and r.get("outcome", "ok") == "ok" and (r.get("ids") or {}).get("newPid"):
            new_people.append({"journal_id": r["id"], "tree_id": str(r["tree_id"]), "person_id": str(r["ids"]["newPid"]), "added_to": str(r["person_id"])})
    out = {"ok": True, "classification": "journal-summary", "since": since, "writes": len(rows), "by_op": by_op,
           "failed": [r["id"] for r in rows if r.get("outcome") == "failed"],
           "unknown_outcomes": [r["id"] for r in rows if r.get("outcome") == "unknown"],
           "detaches_and_removals": [{"journal_id": r["id"], "op": r["op"], "person_id": str(r["person_id"])} for r in rows
                                     if r["op"] in ("fact-detach-source", "fact-remove", "citation-remove", "person-remove", "relationship-remove", "weblink-remove", "tag-remove", "media-remove")],
           "new_people": new_people, "people": sorted(people.values(), key=lambda e: e["journal_ids"][0])}
    if names:
        from .discovery import person
        cache = {}
        for entry in out["people"] + [{"tree_id": n["tree_id"], "person_id": n["person_id"], "_n": n} for n in new_people]:
            key = (entry["tree_id"], entry["person_id"])
            if key not in cache:
                got = person(tree_id=int(key[0]), person_id=int(key[1]))
                cache[key] = got.get("name") if got.get("ok") else None
            (entry.get("_n") or entry)["name"] = cache[key]
    return out


def command(*, action, entry_id=None, dry_run=True, all_entries=False, resolve_as=None, since=None, names=False, tree_id=None):
    """list: outstanding creations (ids only). undo: run (or dry-run) the inverse of one or all."""
    if action == "summary":
        return summary(since=since, names=names, tree_id=tree_id)
    if action in ("resolve", "verify"):
        row = next((r for r in _rows() if r.get("id") == entry_id), None) if entry_id is not None else None
        if row is None:
            return {"ok": False, "classification": "usage-error", "dispatch_attempted": False}
        if action == "resolve":
            if resolve_as not in ("ok", "failed"):
                return {"ok": False, "classification": "usage-error", "dispatch_attempted": False}
            _append({"resolved": entry_id, "as": resolve_as, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return {"ok": True, "classification": "resolved", "id": entry_id, "as": resolve_as}
        from .discovery import person
        out = person(tree_id=row["tree_id"], person_id=row["person_id"])   # current state, living people redacted
        return {"ok": out.get("ok") is True, "classification": "verify", "entry": {k: row.get(k) for k in ("id", "ts", "op", "tree_id", "person_id", "outcome", "ids")},
                "person": out, "next": "if the change is there: `journal resolve --id N --as ok`; if not: `--as failed`"}
    if action == "undo" and entry_id is None and not all_entries:
        return {"ok": False, "classification": "specify-id-or-all", "dispatch_attempted": False}
    todo = [r for r in outstanding() if entry_id is None or r["id"] == entry_id]
    if action == "list":
        return {"ok": True, "classification": "journal", "outstanding": [
            {k: r[k] for k in ("id", "ts", "op", "tree_id", "person_id", "ids")} for r in todo],
            "manual_cleanup": [{k: r[k] for k in ("id", "op", "tree_id", "person_id", "ids")} for r in manual()]}
    results = []
    for r in reversed(todo):  # newest first, so children go before parents
        u = r["undo"]
        if all_entries and entry_id is None and r["tree_id"] not in config.sandbox_trees():
            results.append({"id": r["id"], "undo_op": u["op"], "skipped": "real-tree-needs-explicit-id"})
            continue
        step = {"id": r["id"], "undo_op": u["op"]}
        if dry_run:
            results.append({**step, "dry_run": True})
            continue
        from .ops import write
        out = write(op=u["op"], tree_id=r["tree_id"], person_id=u["person_id"],
                         dry_run=False, confirm_tree=r["tree_id"], **({"assertion_id": u["assertion_id"]} if "assertion_id" in u else {}), **u["fields"])
        results.append({**step, "ok": out.get("ok"), "status": out.get("status")})
        if out.get("ok"):
            _append({"undo_of": r["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            if out.get("journal_id"):       # the undo's own write must not show up as something else to undo
                _append({"resolved": out["journal_id"], "as": "ok", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    live = [x for x in results if "skipped" not in x and not x.get("dry_run")]
    out = {"ok": all(x.get("ok", True) for x in results if "skipped" not in x), "classification": "undo-dry-run" if dry_run else "undone",
           "results": results}
    if live:        # the same state words a write uses: did anything change, did a step fail
        out["state"] = "changed" if any(x.get("ok") for x in live) else "unchanged"
    return out
