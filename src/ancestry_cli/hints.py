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


def parse_hints(body):
    """The hints on a person's page, in page order: [{hint_id, record_id, collection_id}] (record_id None if not a record)."""
    text = html.unescape(body)
    ids, starts = [], []
    for m in re.finditer(r'"HintId"\s*:\s*"(\d{8,})"|hintId=(\d{8,})', text):
        hint_id = m.group(1) or m.group(2)
        if hint_id not in ids:
            ids.append(hint_id)
            starts.append(m.start())
    starts.append(len(text))
    out = []
    for i, hint_id in enumerate(ids):
        gid = re.search(r"recordGid[\"']?\s*[:=]\s*[\"']?(\d+):(\d+)", text[starts[i]:starts[i + 1]])
        out.append({"hint_id": hint_id, "record_id": gid.group(1) if gid else None, "collection_id": gid.group(2) if gid else None})
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
    body: dict = None
    preview: dict = None
    sent: bool = False

    @property
    def source_gid(self):
        return f"{self.record_id}:{self.collection_id}"

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
    a.body = merge_payload._build_upload_body(json.loads(comparison.text), hint_id=a.hint_id, person_gid=a.person_gid,
                                              source_gid=a.source_gid, cite_only=a.cite_only)
    node = a.body["Nodes"]["1:99"]
    a.preview = {"events_cited_existing": sum(1 for e in node["Events"] if "AssertionId" in e),
                 "events_new_from_record": sum(1 for e in node["Events"] if "AssertionId" not in e),
                 "names_cited": len(node["Names"]), "cite_only": a.cite_only}


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


def _journal(a, outcome, req_hash=None):
    """Append the journal row; returns its id (None if it could not be written). Never raises."""
    try:
        from .journal import record
        ids = {"hintId": a.hint_id, "recordId": a.record_id, "collectionId": a.collection_id}
        return record("hint-accept", a.tree_id, a.person_id, {"hint_id": a.hint_id}, ids, outcome, req_hash=req_hash)
    except Exception:
        return None


def _commit(a, poll_seconds, req_hash):
    """Upload, wait, confirm. From the first POST on, a failure is an unknown outcome."""
    a.lease.check()
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
    return {"ok": True, "classification": "accepted", "dispatch_attempted": True, "state": "changed", "preview": a.preview,
            "journal_id": journal_id}


def _guards(tree_id, person_id, hint_id, cite_only, confirm_tree, force):
    """The live-write guards. Returns (failure result or None, request hash)."""
    from .journal import find_duplicate, request_hash
    if not config.writable(tree_id):
        return failure("tree-not-writable"), None
    if config.needs_confirmation(tree_id, confirm_tree):
        return failure("confirm-tree-required"), None
    req_hash = request_hash("hint-accept", tree_id, person_id, {"hint_id": hint_id, "cite_only": cite_only})
    duplicate = None if force else find_duplicate(req_hash)
    return (failure("duplicate-write", journal_id=duplicate) if duplicate is not None else None), req_hash


def command(*, action, tree_id, person_id, hint_id=None, cite_only=False, dry_run=True, bridge=None, poll_seconds=30,
            confirm_tree=None, force=False, include_living=False):
    """`list` shows the person's hints; `accept` accepts one (dry-run unless dry_run=False)."""
    if type(tree_id) is not int or type(person_id) is not int or not config.tree_allowed(tree_id):
        return failure("configuration-error")
    hint_id = str(hint_id) if hint_id else None
    if action == "accept" and not hint_id:
        return failure("hint-id-required")
    req_hash = None
    if action == "accept" and not dry_run:
        refusal, req_hash = _guards(tree_id, person_id, hint_id, cite_only, confirm_tree, force)
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
            if action == "parents":
                evalv = _EVAL.search(json.dumps(data))
                if not evalv:
                    return failure("eval-version-unresolved")
                found = _parents(inner, tree_id, person_id, evalv.group(1), include_living)
                return {"ok": True, "classification": "potential-parents", "dispatch_attempted": True, "state": "unchanged",
                        "tree_id": str(tree_id), "person_id": str(person_id), "count": len(found), "suggestions": found,
                        "note": "Accepting a suggested parent is not built: no capture of the accept request exists yet."}
            chosen = next((h for h in hints if h["hint_id"] == hint_id), None)
            if not chosen or not chosen["record_id"]:
                return failure("hint-not-found-or-not-a-record")
            evalv = _EVAL.search(json.dumps(data))
            if not evalv:
                return failure("eval-version-unresolved")
            accept = _Accept(inner, lease, tree_id, person_id, hint_id, chosen["record_id"], chosen["collection_id"],
                             evalv.group(1), cite_only)
            _plan(accept)
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
