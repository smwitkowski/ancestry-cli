"""Send one write through the signed-in browser: preflight, snapshot, dispatch, readback, journal.

The steps, in order (each is a small function below):
  _open      lock + lane + preflight (status, edit rights, account) + before-snapshot for operations that change things
  _prepare   fill in what the page knows (anchor ids, a fact's current values, a person's name)
  _dispatch  send exactly one request (or the three-request media upload)
  _finish    judge the response, read the result back, journal it, and shape the result

Output is status, response key names and ids only, never body values. Chrome supplies the session; no cookie or
header is read. If an exception happens after the request left, the outcome is recorded as unknown, never as failed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from . import config
from . import runtime as rt
from .ops import SPECS, WriteRequestError, build, succeeded
from .runtime import (
    LaneError,
    actor_from_page,
    classify_exception,
    classify_preflight,
    failure,
    open_lane,
    shape,
    trip_breaker,
)
from .snapshots import SNAPSHOT_OPS, person_data, restore_fields, snapshot_from_page
from .snapshots import diff as snapshot_diff
from .snapshots import save as save_snapshot

_BASE = "https://www.ancestry.com"
_JOURNAL_FIELDS = ("assertion_id", "citation_id", "hint_id", "media_id", "web_link_id")   # ids only, never names or text


class _Refused(Exception):
    """A guard stopped the write before anything was sent."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class _Write:
    op: str
    tree_id: int
    person_id: int
    lease: object = None
    inner: object = None
    facts_url: str = ""
    page_text: str = ""
    actor: str = ""
    before: dict | None = None
    snapshot_path: str | None = None
    sent: bool = False
    progress: dict | None = None        # for operations made of several requests: {"done": [...], "failed_at": step}


# ---------------------------------------------------------------------------------------------------- response -> ids
_ID_FIELDS = {"newPid": "newPid", "AttributeIds": "AttributeIds", "addedTags": "addedTags", "removedTags": "removedTags"}


def _numeric(value):
    values = value if isinstance(value, list) else [value]
    return value is not None and all(isinstance(x, str) and x.isdigit() for x in values)


def _ids(data):
    """The ids a caller needs to chain the next command or to undo this one (opaque values only)."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key in _ID_FIELDS:
        if _numeric(data.get(key)):
            out[key] = data[key]
    if type(data.get("ErrorCode")) is int:
        out["ErrorCode"] = data["ErrorCode"]                       # the app-level result; 0 means success
    attaches = data.get("attaches")
    if isinstance(attaches, list) and attaches and isinstance(attaches[0], dict):
        for source, name in (("id", "mediaId"), ("treeMediaId", "treeMediaId")):
            if isinstance(attaches[0].get(source), str):
                out[name] = attaches[0][source]
    gid = data.get("gid")
    if isinstance(gid, dict) and isinstance(gid.get("v"), str) and gid["v"].split(":")[0].isdigit():
        out["gid"] = gid["v"].split(":")[0]                         # source-create: the sourceId; citation-add: the citation id
    rows = data.get("result")
    if isinstance(rows, list) and rows and isinstance(rows[-1], dict) and isinstance(rows[-1].get("id"), str):
        out["webLinkId"] = rows[-1]["id"]                           # weblinkadd lists the person's links; the last is new
    return out


# ---------------------------------------------------------------------------------------------------- reads the sender needs
def _anchor_ids(text):
    """nameId/genderId of the anchor person, from GET .../add?rel=<relation> (first occurrence in the response)."""
    text = text.replace('\\"', '"')
    name = re.search(r'"nameId":"(\d+)"', text)
    gender = re.search(r'"genderId":"(\d+)"', text[name.end():]) if name else None
    if not (name and gender):
        raise WriteRequestError("anchor-ids-unresolved")
    return name.group(1), gender.group(1)


def _anchor_gender(page_text):
    """Female / Male from the person's own Gender fact (the add form sends it back)."""
    try:
        pr = person_data(page_text)["person"]["PersonResearch"]
        return next((f.get("Value") for f in pr.get("PersonFacts", []) if f.get("Type") == 44 and f.get("Value") in ("Female", "Male")), "")
    except Exception:
        return ""


def _parent_set(w, fields):
    """(fatherId, motherId) for a new child or sibling, as the site's own form chooses them."""
    pr = person_data(w.page_text)["person"]["PersonResearch"]
    family = pr.get("PersonFamily") or {}
    ids = lambda key: [str(x["Id"]) for x in family.get(key) or [] if isinstance(x, dict) and x.get("Id")]
    if fields["relation"] in ("Brother", "Sister"):
        fathers, mothers = ids("Fathers"), ids("Mothers")
        return (fathers[0] if fathers else ""), (mothers[0] if mothers else "")
    spouses = ids("Spouses")
    other = str(fields.get("other_parent") or "")
    if other == "unknown":
        other = ""
    elif not other:
        if len(spouses) > 1:
            raise WriteRequestError("invalid-write-request", [{"field": "other_parent", "issue": "missing", "valid_values": spouses + ["unknown"]}])
        other = spouses[0] if spouses else ""
    elif other not in spouses:
        raise WriteRequestError("invalid-write-request", [{"field": "other_parent", "issue": "invalid", "valid_values": spouses + ["unknown"]}])
    me = str(w.person_id)
    return (me, other) if fields.get("anchor_gender") == "Male" else (other, me)


def _read_note(w):
    url = f"{_BASE}/family-tree/person/workspace/user/{w.actor}/tree/{w.tree_id}/person/{w.person_id}/getpersonnotes"
    resp = w.inner.get(url, timeout=30, allow_redirects=False, writes_ok=False)
    note = json.loads(resp.text).get("note")
    return note if isinstance(note, str) else ""


# ---------------------------------------------------------------------------------------------------- step 1: open
def _open(w, bridge):
    """Lane, then the person page as a preflight: status first, then edit rights, then the account."""
    bridge = bridge or rt.load_bridge()
    w.lease = open_lane(bridge)
    w.inner = bridge.ChromeBrowserSession(cdp_url=w.lease.base, target_id=w.lease.target_id)
    w.facts_url = f"{_BASE}/family-tree/person/tree/{w.tree_id}/person/{w.person_id}/facts"
    page = w.inner.get(w.facts_url, timeout=30, allow_redirects=False, writes_ok=False)
    code = classify_preflight(getattr(page, "status_code", None), page.text)
    if code:
        if code == "bot-challenge":
            trip_breaker()
        raise _Refused(code)
    w.page_text = page.text
    try:
        if person_data(w.page_text)["person"]["PersonResearch"].get("HasTreeEditRights") is False:
            raise _Refused("no-edit-rights")
    except _Refused:
        raise
    except Exception:
        pass            # best effort: if the page has an unexpected shape the server still enforces rights
    w.actor = actor_from_page(w.page_text, config.account_id())
    if w.op in SNAPSHOT_OPS:        # fail closed: no restore data, no write
        try:
            w.before = snapshot_from_page(w.page_text, w.tree_id, w.person_id)
            if w.op == "note-set":
                w.before["note_before"] = _read_note(w)
            w.snapshot_path = save_snapshot(w.before, w.op)
        except Exception:
            raise _Refused("snapshot-failed") from None


# ---------------------------------------------------------------------------------------------------- step 2: prepare
def _merge_source(w, fields):
    """An edit is a patch: fill the fields not given from the source's current values (read from the tree's source list)."""
    from .sources import current
    row = current(w.inner, w.actor, w.tree_id, fields["source_id"])
    if row is None:
        raise _Refused("source-not-found")
    have = {"title": row.get("title"), "author": row.get("auth"), "publisher": row.get("pub"), "publication_place": row.get("publ"),
            "publication_date": row.get("pubd"), "call_number": row.get("cn"), "refn": row.get("refn"),
            "note": _unwrap(row.get("note")), "repository_id": str((row.get("rgid") or {}).get("v", "")).split(":")[0]}
    return {**{k: v for k, v in have.items() if v}, **fields}


def _unwrap(note):
    return re.sub(r"</?line>", "", note) if isinstance(note, str) else note


def _check_source_title(w, fields):
    """Refuse to delete a source unless its live title matches expect_title (or this tool's journal created it)."""
    from .journal import _rows
    expected = fields.get("expect_title")
    ours = any(r.get("op") == "source-create" and r.get("tree_id") == w.tree_id and str(r.get("ids", {}).get("gid")) == str(fields["source_id"])
               for r in _rows())
    if expected is None and not ours:
        raise _Refused("source-title-required")
    path = f"/family-tree/person/sourceedit/user/{w.actor}/tree/{w.tree_id}/source/{fields['source_id']}"
    got = w.inner.request("GET", _BASE + path, timeout=30, allow_redirects=False,
                          _validated_endpoint={"method": "GET", "path": path, "side_effect": False})
    try:
        title = json.loads(got.text)["title"]
    except Exception:
        raise _Refused("source-not-found") from None
    if expected is not None and title != expected:
        raise _Refused("source-title-mismatch")


def _prepare(w, fields):
    """Fill in what only the page knows. Returns the completed fields."""
    fields = dict(fields)
    if w.op == "person-remove" and not fields.get("name"):
        fields["name"] = w.before["name"]                  # read from the page, not stored anywhere
    elif w.op == "relative-add":
        path = f"/family-tree/person/addedit/user/{w.actor}/tree/{w.tree_id}/person/{w.person_id}/add"
        rel = {"Son": "child", "Daughter": "child"}.get(fields["relation"], fields["relation"].lower())
        got = w.inner.request("GET", _BASE + path, params={"rel": rel}, timeout=30, allow_redirects=False,
                              _validated_endpoint={"method": "GET", "path": path, "side_effect": False})
        fields["name_id"], fields["gender_id"] = _anchor_ids(got.text)
        fields.setdefault("anchor_gender", _anchor_gender(w.page_text))
        if fields["relation"] in ("Son", "Daughter", "Brother", "Sister"):
            fields["father_id"], fields["mother_id"] = _parent_set(w, fields)
    elif w.op == "source-delete":
        _check_source_title(w, fields)
    elif w.op == "source-edit":
        fields = _merge_source(w, fields)
    elif w.op == "fact-edit":
        current = restore_fields(w.before, fields.get("assertion_id"))     # an edit is a patch: keep what was not given
        if current is None:
            raise _Refused("fact-not-found")
        fields = {**current, **fields}
    return fields


# ---------------------------------------------------------------------------------------------------- step 3: dispatch
def _media_send(w, fields, attach_url, headers):
    """stoken GET -> binary stream PUT -> attach POST. Returns (last response, journal extras)."""
    import uuid

    from .media import NAMESPACE, attach_body, media_details
    data, sha, mime, width, height, ext = media_details(fields["file"])
    media_id = str(uuid.uuid4())
    extra = {"sha256": sha, "bytes": len(data), "file": fields["file"].rsplit("/", 1)[-1], "mediaId": media_id}
    token = w.inner.get(f"{_BASE}/api/media/upload/mapi/tree/{w.tree_id}/media/{media_id}/stoken", timeout=30,
                        allow_redirects=False, writes_ok=False)
    stoken = json.loads(token.text)["stoken"]
    w.progress = {"done": ["token"], "failed_at": "stream"}
    w.lease.check()
    put = w.inner.put_binary(f"{_BASE}/api/media/upload/v2/stream/namespaces/{NAMESPACE}/media/{media_id}", data=data,
                             content_type=mime, timeout=120,
                             params={"client": "trees-uploadclient", "securityToken": stoken, "format": "json"})
    if not (isinstance(getattr(put, "status_code", None), int) and 200 <= put.status_code < 300):
        return put, extra
    w.progress = {"done": ["token", "stream"], "failed_at": "attach"}        # the image is stored but not yet attached
    body = attach_body(media_id, fields["title"], mime, width, height, len(data), ext)
    resp = w.inner.post(attach_url, data=json.dumps(body).encode(), headers=headers, timeout=60, allow_redirects=False, writes_ok=True)
    if isinstance(getattr(resp, "status_code", None), int) and 200 <= resp.status_code < 300:
        w.progress = {"done": ["token", "stream", "attach"], "failed_at": None}
    return resp, extra


def _then(w, step, first, headers):
    """A create that needs a second request to save its other fields. Returns the first response when both worked, so the
    operation's success test still applies; otherwise the failing response, with progress saying how far it got."""
    status = getattr(first, "status_code", None)
    try:
        new_id = _ids(json.loads(first.text)).get("gid")
    except Exception:
        new_id = None
    if not (isinstance(status, int) and 200 <= status < 300 and new_id):
        return first
    w.progress = {"done": ["create"], "failed_at": "details"}        # created, but the extra fields are not saved yet
    w.lease.check()
    second = w.inner.put(_BASE + step["path"].replace("{id}", new_id), data=json.dumps(step["body"]).encode(), headers=headers,
                         timeout=30, writes_ok=True)
    code = getattr(second, "status_code", None)
    if isinstance(code, int) and 200 <= code < 300:
        w.progress = {"done": ["create", "details"], "failed_at": None}
        return first
    return second


def _dispatch(w, req, fields):
    """Send the request. Returns (response, journal extras). From here on an exception means an unknown outcome."""
    url = _BASE + req["path"]
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Referer": w.facts_url,
               "Origin": _BASE, "X-Requested-With": "XMLHttpRequest"}
    if req.get("text_plain"):
        headers["Content-Type"] = "text/plain;charset=UTF-8"
    w.lease.check()
    w.sent = True
    body = json.dumps(req["body"]).encode() if req["body"] is not None else None
    if w.op == "media-upload":
        return _media_send(w, fields, url, headers)
    if req["method"] == "DELETE":
        return w.inner.delete(url, headers=headers, timeout=30, writes_ok=True), {}
    if req["method"] == "PATCH":
        return w.inner.patch(url, data=body, headers=headers, timeout=30, writes_ok=True), {}
    if req["method"] == "PUT":
        return w.inner.put(url, data=body, headers=headers, timeout=30, writes_ok=True), {}
    if req["method"] == "POST":
        resp = w.inner.post(url, data=body, params=req.get("query"), headers=headers, timeout=30, allow_redirects=False,
                            writes_ok=True)
        if req.get("then"):
            return _then(w, req["then"], resp, headers), {}
        return resp, {}
    query = "?" + urlencode(req["query"]) if req.get("query") else ""
    return w.inner.get(url + query, timeout=30, allow_redirects=False, writes_ok=True), {}


# ---------------------------------------------------------------------------------------------------- step 4: finish
def _journal(w, fields, ids, outcome, req_hash):
    """Append the journal row. Returns (error name or None, row id or None); it never raises: the write already happened."""
    try:
        from .journal import record
        row_id = record(w.op, w.tree_id, w.person_id, {k: v for k, v in fields.items() if k in _JOURNAL_FIELDS}, ids, outcome,
                        w.snapshot_path, req_hash)
        return None, row_id
    except Exception as exc:
        return type(exc).__name__, None


def _explicitly_rejected(status, data):
    """True only when the server clearly said "no": an HTTP 4xx, or a body with an explicit negative signal.
    Anything else that is not a confirmed success may have been applied, so it is an unknown outcome."""
    if isinstance(status, int) and 400 <= status < 500:
        return True
    return isinstance(data, dict) and (data.get("ErrorCode") not in (None, 0) or data.get("success") is False
                                       or data.get("status") is False
                                       or (type(data.get("statusCode")) is int and data["statusCode"] >= 400))


def _readback(w, fields):
    """What changed, as ids and field names (never values)."""
    try:
        if w.op == "note-set":
            return {"note_matches": _read_note(w) == fields["text"]}
        if w.before is not None and w.op != "person-remove":
            after = w.inner.get(w.facts_url, timeout=30, allow_redirects=False, writes_ok=False)
            return snapshot_diff(w.before, snapshot_from_page(after.text, w.tree_id, w.person_id))
    except Exception:
        return {"error": "readback-failed"}
    return None


def _finish(w, resp, extra, fields, req_hash, streams):
    status = getattr(resp, "status_code", None)
    try:
        data = json.loads(resp.text) if str(resp.text).strip() else {}        # an empty body is {}
        response_shape, ids = shape(data), _ids(data)
    except Exception:
        data, response_shape, ids = None, None, {}
    if fields.get("hint_id"):
        ids["hintId"] = str(fields["hint_id"])
    ids = {**ids, **extra}
    ok = succeeded(w.op, status, data)
    readback = _readback(w, fields) if ok else None
    leaked = any(s.getvalue() for s in streams)
    outcome = "ok" if ok else ("failed" if _explicitly_rejected(status, data) else "unknown")
    journal_error, journal_id = _journal(w, fields, ids, outcome, req_hash)
    details = {"status": status, "response_shape": response_shape}
    if isinstance(data, dict) and type(data.get("ErrorCode")) is int:
        details["server_error_code"] = data["ErrorCode"]                      # the server's own integer code, never its text
    progress = {"progress": w.progress} if w.progress else {}
    if leaked:
        return failure("privacy-incident", dispatched=True, journal_id=journal_id)
    if outcome == "unknown":
        return failure("unknown-outcome", dispatched=True, journal_id=journal_id, status=status, response_shape=response_shape,
                       details=details, **progress)
    if not ok:                      # rejected by the server: nothing changed (a half-done media upload leaves an unattached image)
        warnings = [{"code": "orphan-media", "message": "The image was uploaded but not attached; it is not visible in the tree."}] \
            if w.progress and w.progress.get("failed_at") == "attach" else []
        return failure("send-failed", dispatched=True, state="unchanged", journal_id=journal_id, status=status,
                       response_shape=response_shape, ids=ids, details=details, **progress, **({"warnings": warnings} if warnings else {}))
    out = {"ok": True, "classification": "sent", "dispatch_attempted": True, "state": "changed", "status": status,
           "response_shape": response_shape, "ids": ids, "journal_id": journal_id}
    if ids.get("newPid") and w.op == "relative-add":        # relative-link echoes the anchor id in newPid
        out["new_person_id"] = ids["newPid"]
    if w.snapshot_path:
        out["snapshot"] = w.snapshot_path.rsplit("/", 1)[-1]
    warnings = []
    if readback is not None:
        out["readback"] = readback
        if readback.get("error"):
            warnings.append({"code": "readback-failed", "message": "The write succeeded but the result could not be read back to confirm."})
    if journal_error:
        out["journal_error"] = journal_error          # kept for compatibility
        warnings.append({"code": "journal-error", "message": "The write happened but could not be journaled; record it by hand."})
    if progress:
        out.update(progress)
    if warnings:
        out["warnings"] = warnings
    return out


# ---------------------------------------------------------------------------------------------------- entry point
def send(*, op, tree_id, person_id, bridge=None, req_hash=None, **fields):
    """Send one write. Never raises; returns {ok, classification, dispatch_attempted, ...}."""
    if op not in SPECS or not config.tree_allowed(tree_id):
        return failure("configuration-error")
    w = _Write(op, tree_id, person_id)
    with rt.quiet() as streams:
        try:
            with rt.lock("ancestry"):
                _open(w, bridge)
                fields = _prepare(w, fields)
                req = build(op, tree_id=tree_id, person_id=person_id, actor=w.actor, **fields)
                resp, extra = _dispatch(w, req, fields)
                return _finish(w, resp, extra, fields, req_hash, streams)
        except _Refused as refused:
            return failure(refused.code)
        except Exception as exc:
            if w.sent:      # a request left but the result is unknown: record that, and never call it a failure
                _, journal_id = _journal(w, fields, {}, "unknown", req_hash)
                return failure("unknown-outcome", dispatched=True, journal_id=journal_id)
            if isinstance(exc, LaneError):
                return failure(exc.code, **exc.detail)
            if isinstance(exc, WriteRequestError):
                return failure(exc.code, **({"problems": exc.problems} if exc.problems else {}))
            return failure(classify_exception(exc) or "native-operation-failed")
