"""List a person's hints and accept one record hint.

Accepting follows the web app's own merge flow:
    GET comparison -> POST merge/job/upload -> poll merge/job/status -> POST merge/job/post/confirmation
`plan` runs the first step and builds the upload body (a dry-run reports counts only); `commit` runs the rest.
The upload body comes from `merge_payload` (vendored from an earlier prototype), so field selection matches the
web app's default: new facts are added, existing facts are cited, no alternate names are created.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass

from . import config, merge_payload
from . import runtime as rt
from .runtime import (
    LaneError,
    classify_exception,
    classify_preflight,
    failure,
    trip_breaker,
)

_BASE = "https://www.ancestry.com"
_EVAL = re.compile(r"hintsui-eval/(1\.0\.0-[0-9a-z]+)/")      # the web app's deploy version; it changes, so it is read each time


_BOILER = ("Compare details", "In this record", "Status", "In your tree")


def _card_text(card):
    text = re.sub(r"<[^>]+>", "|", card)
    parts = []
    for piece in (x.strip() for x in text.split("|")):
        if piece and piece not in ("New", "-") and (not parts or parts[-1] != piece):
            parts.append(piece)
    return parts


def _card_fields(card):
    """What one hint card says: kind, title, category, record, Ancestry's score, what it would add and a short summary."""
    attr = lambda name: (re.search(rf'data-{name}="([^"]*)"', card) or [None, None])[1]
    ube = re.search(r'data-ube="(\{.*?\})"', card)
    ube = json.loads(ube.group(1)) if ube else {}
    title = re.search(r'class="[^"]*hintTitle[^"]*">(.*?)</h2>', card, re.DOTALL)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title.group(1))).strip() if title else None
    parts = _card_text(card)
    cut = next((i for i, x in enumerate(parts) if x in ("Review", "Ignore")), len(parts))
    body = [x for x in parts[:cut] if x != title and x != attr("databasecategory")]
    return {"kind": "record" if attr("objectid") else "tree", "title": title, "category": attr("databasecategory"),
            "match_score": ube.get("matchScore"), "new_facts": ube.get("numberOfNewAssertions"),
            "new_family_members": ube.get("numberOfNewFamilyMembers"),
            "ai_extracted": any("artificial intelligence" in x for x in parts),
            "summary": " | ".join(x for x in body if "artificial intelligence" not in x and x != "Learn more" and x not in _BOILER)[:300] or None}


def parse_hints(body):
    """The hints on a person's page, in page order: hint_id, record_id, collection_id (None if not a record) and the card's content."""
    text = html.unescape(body)
    ids, starts = [], []
    for m in re.finditer(r'"HintId"\s*:\s*"(\d{8,})"|hintId=(\d{8,})', text):
        hint_id = m.group(1) or m.group(2)
        if hint_id not in ids:
            ids.append(hint_id)
            starts.append(m.start())
    starts.append(len(text))
    cards = {}
    for card in re.split(r'(?=<section class="hintCard)', text)[1:]:
        hid = re.search(r'data-hintId="(\d+)"', card)
        if hid:
            cards[hid.group(1)] = card
    out = []
    for i, hint_id in enumerate(ids):
        gid = re.search(r"recordGid[\"']?\s*[:=]\s*[\"']?(\d+):(\d+)", text[starts[i]:starts[i + 1]])
        row = {"hint_id": hint_id, "record_id": gid.group(1) if gid else None, "collection_id": gid.group(2) if gid else None}
        if hint_id in cards:
            row.update(_card_fields(cards[hint_id]))
        out.append(row)
    return out


def _headers(tree_id, person_id, post=False):
    headers = {"Accept": "application/json, text/plain, */*", "Origin": _BASE,
               "Referer": f"{_BASE}/family-tree/person/tree/{tree_id}/person/{person_id}/hints"}
    if post:
        headers["Content-Type"] = "text/plain;charset=UTF-8"          # the merge UI sends text/plain
    return headers


@dataclass
class _Accept:
    inner: object
    lease: object
    tree_id: int
    person_id: int
    hint_id: str
    record_id: str
    collection_id: str
    evalv: str
    cite_only: bool
    bind_to: tuple = None
    body: dict = None
    plan: dict = None
    available: list = None
    preview: dict = None
    sent: bool = False

    source_override: str = None

    @property
    def source_gid(self):
        return self.source_override or f"{self.record_id}:{self.collection_id}"

    @source_gid.setter
    def source_gid(self, value):
        self.source_override = value

    @property
    def person_gid(self):
        return f"{self.person_id}:1030:{self.tree_id}"

    @property
    def merge_url(self):
        return f"{_BASE}/api/hintsui-api/trees/{self.tree_id}/merge/job"


def _node_name(node):
    n = ((node or {}).get("Name") or {}).get("Record") or {}
    return " ".join(x for x in (n.get("Given"), n.get("Surname"), n.get("Suffix")) if x) or None


def _year(text):
    m = re.search(r"\b(\d{4})\b", str(text or ""))
    return int(m.group(1)) if m else None


def parse_review(review, role, hint, include_living=False):
    """One suggested parent, from the hint's review view: facts, the records behind it and the family in the source tree."""
    info = review.get("Info") or {}
    birth, death = info.get("Birth") or {}, info.get("Death") or {}
    by, dy = _year(birth.get("Date")), _year(death.get("Date"))
    living = bool(by and by > time.gmtime().tm_year - 100 and not dy)
    hide = living and not include_living
    nodes = review.get("Family") or {}
    primary = next((n for n in nodes.values() if n.get("IsPrimaryNode")), {})
    fam = primary.get("Family") or {}
    names = lambda ids: [nm for nm in (_node_name(nodes.get(i)) for i in ids if i) if nm]
    kids = [c for u in fam.get("FamilyUnits") or [] for c in u.get("Children") or []]
    spouses = [x for u in fam.get("FamilyUnits") or [] for x in (u.get("Wife"), u.get("Husband")) if x]
    records = []
    for r in review.get("Records") or []:
        gid = str(r.get("Gid") or "").split(":")
        records.append({"title": r.get("Title"), "record_id": gid[0] if gid else None, "collection_id": gid[1] if len(gid) > 1 else None,
                        "has_image": bool(r.get("ImageId")), "fields": {f.get("Label"): f.get("Value") for f in (r.get("Fields") or [])[:8]}})
    src = str(hint.get("SourceGid") or "").split(":")
    return {"role": role, "hint_id": str(hint.get("HintId")), "source_person_id": src[0] or None,
            "source_tree_id": src[2] if len(src) > 2 else None, "possibly_living": living,
            "name": None if hide else info.get("Name"), "gender": info.get("Gender"),
            "birth": None if hide else {"date": birth.get("Date"), "place": birth.get("Location")},
            "death": None if hide else {"date": death.get("Date"), "place": death.get("Location")},
            "records": [] if hide else records,
            "source_tree_family": None if hide else {"parents": names([fam.get("Father"), fam.get("Mother")]), "spouses": names(spouses),
                                                      "children": names(kids), "siblings": names(fam.get("Siblings") or [])},
            "note": "A suggestion from another member's tree: a lead, never proof. Read the records behind it."}


def _parents(inner, tree_id, person_id, evalv, include_living):
    from .snapshots import person_data
    page = inner.get(f"{_BASE}/family-tree/person/tree/{tree_id}/person/{person_id}/facts", timeout=60, allow_redirects=False, writes_ok=False)
    code = classify_preflight(getattr(page, "status_code", None), page.text)
    if code:
        raise LaneError(code)
    pr = person_data(page.text)["person"]["PersonResearch"]
    raw = pr.get("NewPersonHints") or (pr.get("NewPersonHintsData") or {}).get("hints") or []
    out = []
    for hint in raw:
        role = str(hint.get("Role") or "").lower()
        if role not in ("father", "mother"):
            continue
        review = inner.get(f"{_BASE}/api/hintsui-api/person/{person_id}:1030:{tree_id}/review/{hint.get('SourceGid')}",
                           params={"fullRecords": "true", "includePersonsCount": "false", "evalV": evalv},
                           headers=_headers(tree_id, person_id), timeout=60, allow_redirects=False, writes_ok=False)
        if not 200 <= getattr(review, "status_code", 0) < 300:
            out.append({"role": role, "hint_id": str(hint.get("HintId")), "review": "unavailable", "status": review.status_code})
            continue
        out.append(parse_review(json.loads(review.text), role, hint, include_living))
    return out


def _person_hints(inner, tree_id, person_id):
    """Fetch and parse the person's hints page; returns (hints, raw response data). Raises LaneError on a bad page."""
    page = inner.get(f"{_BASE}/hintsui-personhints/api/PersonHintsList",
                     params={"treeId": str(tree_id), "personId": str(person_id), "pagename": "PersonHints"},
                     timeout=60, allow_redirects=False, writes_ok=False)
    code = classify_preflight(getattr(page, "status_code", None), page.text)
    if code:
        raise LaneError(code)
    data = json.loads(page.text)
    return parse_hints(data["html"]["body"]), data


def _plan(a):
    """Fetch the comparison and build the upload body; fills a.body and a.preview."""
    comparison = a.inner.get(f"{_BASE}/api/hintsui-api/trees/{a.tree_id}/persons/{a.person_id}/comparison/",
                             params={"sourceGid": a.source_gid, "restricttorootnode": "false", "type": "Record", "evalV": a.evalv},
                             headers=_headers(a.tree_id, a.person_id), timeout=60, allow_redirects=False, writes_ok=False)
    data = json.loads(comparison.text)
    a.body = merge_payload._build_upload_body(data, hint_id=a.hint_id, person_gid=a.person_gid,
                                              source_gid=a.source_gid, cite_only=a.cite_only, bind_to=a.bind_to)
    a.plan = merge_payload.plan(data, cite_only=a.cite_only, bind_to=a.bind_to)
    a.available = [b["assertion_id"] for b in merge_payload.plan(data, cite_only=True)["will_bind"]]
    node = a.body["Nodes"]["1:99"]
    a.preview = {"events_cited_existing": sum(1 for e in node["Events"] if "AssertionId" in e),
                 "events_new_from_record": sum(1 for e in node["Events"] if "AssertionId" not in e),
                 "names_cited": len(node["Names"]), "cite_only": a.cite_only,
                 "will_bind": a.plan["will_bind"], "will_create": a.plan["will_create"], "record_family_members": a.plan["record_family_members"]}


def _post(a, tail, payload):
    return a.inner.post(f"{a.merge_url}/{tail}", params={"evalV": a.evalv}, data=json.dumps(payload).encode(),
                        headers=_headers(a.tree_id, a.person_id, post=True), timeout=60, allow_redirects=False, writes_ok=True)


def _wait_for_job(a, location, poll_seconds):
    state, deadline = {"pending": True}, time.time() + poll_seconds
    while state.get("pending") and time.time() < deadline:
        status = a.inner.get(f"{a.merge_url}/status", params={"location": location, "evalV": a.evalv},
                             headers=_headers(a.tree_id, a.person_id), timeout=30, allow_redirects=False, writes_ok=False)
        state = json.loads(status.text)
        if state.get("pending"):
            time.sleep(1)
    return state


def _journal(a, outcome, req_hash=None, op="hint-accept"):
    """Append the journal row; returns its id (None if it could not be written). Never raises."""
    try:
        from .journal import record
        ids = {"hintId": a.hint_id, "recordId": a.record_id, "collectionId": a.collection_id}
        return record(op, a.tree_id, a.person_id, {"hint_id": a.hint_id}, ids, outcome, req_hash=req_hash)
    except Exception:
        return None


def _accept_parent(a, dry_run, poll_seconds, req_hash, include_living):
    """Accept a suggested parent: Ancestry copies the other tree's person into this tree and links them as the father or mother.
    This CREATES a new person (it never merges into an existing one); the result reports the new id when Ancestry names it."""
    from .snapshots import person_data
    inner, tree_id, person_id, hint_id, evalv = a.inner, a.tree_id, a.person_id, a.hint_id, a.evalv
    page = inner.get(f"{_BASE}/family-tree/person/tree/{tree_id}/person/{person_id}/facts", timeout=60, allow_redirects=False, writes_ok=False)
    pr = person_data(page.text)["person"]["PersonResearch"]
    raw = pr.get("NewPersonHints") or (pr.get("NewPersonHintsData") or {}).get("hints") or []
    hint = next((h for h in raw if str(h.get("HintId")) == hint_id), None)
    role = str((hint or {}).get("Role") or "").lower()
    if hint is None or role not in ("father", "mother") or not hint.get("SourceGid"):
        return failure("hint-not-found-or-not-a-parent")
    slot = (pr.get("PersonFamily") or {}).get("Fathers" if role == "father" else "Mothers") or []
    if any(m for m in _flatten(slot)):
        return failure("parent-slot-occupied", role=role)
    a.source_gid = str(hint["SourceGid"])               # shadows the record-based property for this flow
    shown = parse_review(json.loads(inner.get(f"{_BASE}/api/hintsui-api/person/{person_id}:1030:{tree_id}/review/{a.source_gid}",
                                             params={"evalV": evalv}, headers=_headers(tree_id, person_id), timeout=60,
                                             allow_redirects=False, writes_ok=False).text), role, hint, include_living)
    preview = {"relation": role, "suggested": {k: shown.get(k) for k in ("name", "birth", "death", "source_tree_family")}}
    if dry_run:
        return {"ok": True, "classification": "dry-run", "dispatch_attempted": False, "state": "unchanged", "preview": preview}
    body = {"hintId": hint_id, "relation": role, "personGid": a.person_gid, "sourcePersonGid": a.source_gid, "parents": {}}
    a.lease.check()
    a.sent = True
    upload = json.loads(_post(a, "upload/nph", body).text)
    state = _wait_for_job(a, upload.get("location"), poll_seconds)
    if state.get("pending") or not state.get("success"):
        return failure("merge-job-failed", dispatched=True, preview=preview, journal_id=_journal(a, "unknown", req_hash, "hint-parent-accept"),
                       progress={"done": ["upload"], "failed_at": "status"})
    payload = upload.get("postConfirmationPayload") or merge_payload._build_confirmation_body(
        hint_id=hint_id, person_gid=a.person_gid, source_gid=a.source_gid)["postConfirmationPayload"]
    ok = bool(json.loads(_post(a, "post/confirmation", {"postConfirmationPayload": payload}).text).get("success"))
    journal_id = _journal(a, "ok" if ok else "unknown", req_hash, "hint-parent-accept")
    if not ok:
        return failure("confirmation-failed", dispatched=True, preview=preview, journal_id=journal_id,
                       progress={"done": ["upload", "status"], "failed_at": "confirmation"})
    again = inner.get(f"{_BASE}/family-tree/person/tree/{tree_id}/person/{person_id}/facts", timeout=60, allow_redirects=False, writes_ok=False)
    fam = person_data(again.text)["person"]["PersonResearch"].get("PersonFamily") or {}
    added = [{"person_id": str(m.get("Id")), "name": m.get("FullName")} for m in _flatten(fam.get("Fathers" if role == "father" else "Mothers") or [])]   # read inside the open lane: a second lock would deadlock
    return {"ok": True, "classification": "accepted-parent", "dispatch_attempted": True, "state": "changed", "preview": preview,
            "created_new_person": True, "parent_now": added, "journal_id": journal_id}


_KEEP = {"names-only": ("Name", "Gender"), "none": ("Name",)}


def _trim_imported(res, tree_id, hint_id, facts):
    """After a parent-accept: the site has copied the other member's person with all its facts. Outside the lane lock (these are
    writes), remove the facts the caller did not ask for, flag what was kept, and leave a note saying where it all came from."""
    from .discovery import person as read_person
    from .ops import write
    pid = next((p["person_id"] for p in res.get("parent_now") or [] if p.get("person_id")), None)
    if not pid:
        res["imported_check"] = "new person not found; check its facts by hand"
        return res
    got = read_person(tree_id=tree_id, person_id=int(pid), include_living=True)
    if not got.get("ok"):
        res["imported_check"] = "could not read the new person back; check its facts by hand"
        return res
    removed, kept, failed = [], [], []
    for f in got["facts"]:
        if facts != "all" and f["type"] not in _KEEP[facts]:
            r = write(op="fact-remove", tree_id=tree_id, person_id=int(pid), assertion_id=f["assertion_id"], dry_run=False,
                      confirm_tree=tree_id, force=True)
            (removed if r.get("ok") else failed).append({"assertion_id": f["assertion_id"], "type": f["type"], "date": f["date"], "place": f["place"]})
        else:
            kept.append({"assertion_id": f["assertion_id"], "type": f["type"], "date": f["date"], "place": f["place"]})
    note = (f"Created by accepting Ancestry's suggested {res['preview']['relation']} (hint {hint_id}) from another member's tree. "
            + ("Only the name"
               + (" and gender" if facts == "names-only" else "")
               + " were kept; every imported date, place and event was removed. " if facts != "all" else
               "Its dates, places and events were copied from that tree and are UNVERIFIED: do not treat them as proven. ")
            + "Nothing here is proven until a source is attached.")
    n = write(op="note-set", tree_id=tree_id, person_id=int(pid), dry_run=False, confirm_tree=tree_id, force=True, text=note)
    res["facts_mode"] = facts
    res["facts_removed"] = removed
    res["facts_remove_failed"] = failed
    res["imported_unverified"] = [{**k, "status": "imported_unverified"} for k in kept if facts == "all" or k["type"] not in ("Name", "Gender")]
    res["note_set"] = bool(n.get("ok"))
    res["new_person_family"] = {k: [m["name"] for m in v] for k, v in (got.get("family") or {}).items() if v}
    return res


def command(**kw):
    """`list` shows the person's hints; `accept` accepts one; `parent-accept` accepts a suggested parent (see _trim_imported)."""
    facts = kw.pop("facts", "names-only") if kw.get("action") == "parent-accept" else None
    kw.pop("facts", None)
    res = _command(**kw)
    if facts is not None and res.get("classification") == "dry-run" and isinstance(res.get("preview"), dict):
        res["preview"]["facts_mode"] = facts
        res["preview"]["note"] = ("The site copies the other member's person with all its facts; after accepting, "
                                  + ("they are all kept and flagged imported_unverified." if facts == "all" else
                                     f"every fact except {' and '.join(_KEEP[facts])} is removed again and a note records the source."))
    if facts is not None and res.get("classification") == "accepted-parent" and kw.get("dry_run") is not True:
        res = _trim_imported(res, kw["tree_id"], kw.get("hint_id"), facts)
    return res


def _flatten(slot):
    out = []
    for m in slot:
        out.extend(m if isinstance(m, list) else [m])
    return out


def _citations(a):
    """{citation_id: [assertion ids]} and {assertion id: fact type} as the person's page shows them now."""
    from .snapshots import person_data
    page = a.inner.get(f"{_BASE}/family-tree/person/tree/{a.tree_id}/person/{a.person_id}/facts", timeout=60, allow_redirects=False, writes_ok=False)
    pr = person_data(page.text)["person"]["PersonResearch"]
    cites = {str(c["CitationId"]): str(c.get("AssertionIds") or "").split() for key in ("PersonSources", "UGCPersonSources") for c in (pr.get(key) or []) if c.get("CitationId")}
    types = {str(f["AssertionId"]): f.get("TypeString") for f in pr.get("PersonFacts", []) if f.get("AssertionId")}
    return cites, types


def _commit(a, poll_seconds, req_hash):
    """Upload, wait, confirm. From the first POST on, a failure is an unknown outcome."""
    a.lease.check()
    try:
        before, _ = _citations(a)
    except Exception:
        before = None
    a.sent = True
    upload = json.loads(_post(a, "upload", a.body).text)
    state = _wait_for_job(a, upload.get("location"), poll_seconds)
    if state.get("pending") or not state.get("success"):
        return failure("merge-job-failed", dispatched=True, preview=a.preview, journal_id=_journal(a, "unknown", req_hash),
                       progress={"done": ["upload"], "failed_at": "status"})
    payload = upload.get("postConfirmationPayload") or merge_payload._build_confirmation_body(
        hint_id=a.hint_id, person_gid=a.person_gid, source_gid=a.source_gid)["postConfirmationPayload"]
    ok = bool(json.loads(_post(a, "post/confirmation", {"postConfirmationPayload": payload}).text).get("success"))
    journal_id = _journal(a, "ok" if ok else "unknown", req_hash)         # unconfirmed = unknown: the upload did go through
    if not ok:
        return failure("confirmation-failed", dispatched=True, preview=a.preview, journal_id=journal_id,
                       progress={"done": ["upload", "status"], "failed_at": "confirmation"})
    out = {"ok": True, "classification": "accepted", "dispatch_attempted": True, "state": "changed", "preview": a.preview,
           "journal_id": journal_id}
    try:        # read back what the new citation is actually bound to
        after, types = _citations(a)
        new = [c for c in after if before is not None and c not in before]
        out["new_citation_ids"] = new
        out["bound_to"] = [{"assertion_id": x, "type": types.get(x)} for c in new for x in after[c]]
        planned = {b["assertion_id"] for b in a.plan["will_bind"]}
        extra = [b["assertion_id"] for b in out["bound_to"] if b["assertion_id"] not in planned]
        if extra:
            out["warnings"] = [{"code": "bound-beyond-plan", "message": "The citation is bound to facts the dry-run did not list.", "assertion_ids": extra}]
    except Exception:
        out["bound_to"] = None
    return out


def _guards(tree_id, person_id, hint_id, cite_only, confirm_tree, force, op="hint-accept", bind_to=None):
    """The live-write guards. Returns (failure result or None, request hash)."""
    from .journal import find_duplicate, request_hash
    if not config.writable(tree_id):
        return failure("tree-not-writable"), None
    if config.needs_confirmation(tree_id, confirm_tree):
        return failure("confirm-tree-required"), None
    req_hash = request_hash(op, tree_id, person_id, {"hint_id": hint_id, "cite_only": cite_only, **({"bind_to": sorted(bind_to)} if bind_to else {})})
    duplicate = None if force else find_duplicate(req_hash)
    return (failure("duplicate-write", journal_id=duplicate) if duplicate is not None else None), req_hash


_BATCH_OPS = ("hint-no", "hint-maybe", "hint-new", "hint-ignore", "hint-restore", "accept")


def batch(*, op, tree_id, person_id, hint_ids, cite_only=False, dry_run=True, confirm_tree=None, force=False):
    """The same change for several hints of one person: one dry-run or one confirmation, one result and one journal row per hint,
    then a read-back of the person's hint list (`still_listed` = hints that are still pending afterwards)."""
    ids = [h for h in dict.fromkeys(str(x).strip() for x in (hint_ids or [])) if h]
    if op not in _BATCH_OPS or not ids or len(ids) > 50:
        return failure("invalid-write-request", problems=[{"field": "op" if op not in _BATCH_OPS else "hint_ids", "issue": "invalid",
                                                           "valid_values": list(_BATCH_OPS) if op not in _BATCH_OPS else "1 to 50 hint ids"}])
    from .ops import write
    results = []
    for hid in ids:
        if op == "accept":
            r = command(action="accept", tree_id=tree_id, person_id=person_id, hint_id=hid, cite_only=cite_only, dry_run=dry_run,
                        confirm_tree=confirm_tree, force=force)
        else:
            r = write(op=op, tree_id=tree_id, person_id=person_id, dry_run=dry_run, confirm_tree=confirm_tree, force=force, hint_id=hid)
        results.append({"hint_id": hid, "ok": r.get("ok"), "classification": r.get("classification"), "journal_id": r.get("journal_id")})
        if not r.get("ok") and not dry_run:
            break           # stop at the first failure: the rest were not attempted
    out = {"ok": all(r["ok"] for r in results) and len(results) == len(ids), "classification": "hint-batch-dry-run" if dry_run else "hint-batch",
           "op": op, "tree_id": str(tree_id), "person_id": str(person_id), "requested": len(ids), "attempted": len(results), "results": results}
    if not dry_run:
        out["state"] = "changed" if any(r["ok"] for r in results) else "unchanged"
        back = command(action="list", tree_id=tree_id, person_id=person_id)
        if back.get("ok"):
            pending = {h["hint_id"] for h in back["hints"]}
            out["still_listed"] = [h for h in ids if h in pending]
    return out


def _command(*, action, tree_id, person_id, hint_id=None, cite_only=False, dry_run=True, bridge=None, poll_seconds=30,
            confirm_tree=None, force=False, include_living=False, bind_to=None, facts="names-only"):
    """`list` shows the person's hints; `accept` accepts one (dry-run unless dry_run=False)."""
    if type(tree_id) is not int or type(person_id) is not int or not config.tree_allowed(tree_id):
        return failure("configuration-error")
    hint_id = str(hint_id) if hint_id else None
    if action in ("accept", "parent-accept") and not hint_id:
        return failure("hint-id-required")
    req_hash = None
    if action in ("accept", "parent-accept") and not dry_run:
        refusal, req_hash = _guards(tree_id, person_id, hint_id, cite_only, confirm_tree, force, "hint-parent-accept" if action == "parent-accept" else "hint-accept", bind_to)
        if refusal:
            return refusal
    accept = None
    try:
        with rt.quiet(), rt.lock("ancestry"):
            bridge = bridge or rt.load_bridge()
            lease = rt.open_lane(bridge)
            inner = bridge.ChromeBrowserSession(cdp_url=lease.base, target_id=lease.target_id)
            hints, data = _person_hints(inner, tree_id, person_id)
            if action == "list":
                return {"ok": True, "classification": "hints", "hints": hints}
            if action == "parent-accept":
                evalv = _EVAL.search(json.dumps(data))
                if not evalv:
                    return failure("eval-version-unresolved")
                accept = _Accept(inner, lease, tree_id, person_id, hint_id, "", "", evalv.group(1), False)
                return _accept_parent(accept, dry_run, poll_seconds, req_hash, include_living)
            if action == "parents":
                evalv = _EVAL.search(json.dumps(data))
                if not evalv:
                    return failure("eval-version-unresolved")
                found = _parents(inner, tree_id, person_id, evalv.group(1), include_living)
                return {"ok": True, "classification": "potential-parents", "dispatch_attempted": True, "state": "unchanged",
                        "tree_id": str(tree_id), "person_id": str(person_id), "count": len(found), "suggestions": found,
                        "note": "Accept one with `hint parent-accept --hint-id H` (creates a new person; explicit approval)."}
            chosen = next((h for h in hints if h["hint_id"] == hint_id), None)
            if not chosen or not chosen["record_id"]:
                return failure("hint-not-found-or-not-a-record")
            evalv = _EVAL.search(json.dumps(data))
            if not evalv:
                return failure("eval-version-unresolved")
            accept = _Accept(inner, lease, tree_id, person_id, hint_id, chosen["record_id"], chosen["collection_id"],
                             evalv.group(1), cite_only, tuple(bind_to) if bind_to else None)
            _plan(accept)
            missing = [x for x in (bind_to or []) if x not in accept.available]
            if missing:
                return failure("bind-to-not-found", unknown=missing, available=accept.available)
            if dry_run:
                return {"ok": True, "classification": "dry-run", "dispatch_attempted": False, "record_id": accept.record_id,
                        "collection_id": accept.collection_id, "preview": accept.preview}
            return _commit(accept, poll_seconds, req_hash)
    except Exception as exc:
        if accept is not None and accept.sent:
            return failure("unknown-outcome", dispatched=True, journal_id=_journal(accept, "unknown", req_hash))
        if isinstance(exc, LaneError):
            if exc.code == "bot-challenge":
                trip_breaker()
            return failure(exc.code, **exc.detail)
        return failure(classify_exception(exc) or "native-operation-failed")
