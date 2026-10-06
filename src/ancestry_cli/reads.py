"""Reads: search, hints, records, and any known read endpoint.

`list` shows every known read endpoint; `get NAME` calls one; `search`, `hints` and `record` are named shortcuts.
Output is the response's shape (keys and types) unless `full` is set. The vendored bridge validates every request
against its inventory of known endpoints, so only those can be called.
"""
from __future__ import annotations

import json
import re

from . import config
from . import runtime as rt
from .runtime import LaneError, actor_from_page, classify_exception, classify_preflight, failure, shape, trip_breaker

_BASE = "https://www.ancestry.com"
_LIST_FIELDS = ("group", "name", "path", "query", "needs", "notes")
_SIDE_EFFECT_READS = frozenset({"start_or_get_export"})      # GETs that start server-side work: never offered as reads


def _endpoints(bridge):
    return [e for e in bridge.ENDPOINTS if e["method"] == "GET" and not e.get("side_effect")]


def _named(action, *, given, surname, birth, death, location, collection, record_id, counts, tree_id, person_id):
    """A named shortcut -> (endpoint name, path params, query params), or None when inputs are missing."""
    if action == "search":
        if not surname:
            return None
        # birth/death/location take the site's own `YYYY[-M-D][_place-slug]` form
        params = {"name": f"{(given or '').replace(' ', '+')}_{surname.replace(' ', '+')}", "priority": "usa", "searchMode": "advanced"}
        params.update({k: v for k, v in (("birth", birth), ("death", death), ("location", location)) if v})
        if collection:
            return "collection_results", {}, {**params, "collections": str(collection)}
        return ("hit_counts" if counts else "search_results"), {}, params
    if action == "hints":
        if tree_id is None or person_id is None:
            return None
        return "person_hints_list", {}, {"treeId": str(tree_id), "personId": str(person_id), "pagename": "PersonHints"}
    if action == "record":
        if not (collection and record_id):
            return None
        return "record_hover_card", {"collectionId": str(collection), "recordId": str(record_id)}, {
            "client": "searchui-resultsui", "viewType": "Hover", "pageName": "ancestry us : search : results",
            "hasOremCorrections": "UNKNOWN"}
    return None


def _fill_path(endpoint, values, inner, tree_id, person_id):
    """Substitute {placeholders}; the account id is read from the person page when the path needs it."""
    path = endpoint["path"]
    if "{userId}" in path and "userId" not in values:
        if tree_id is None or person_id is None:
            raise LaneError("tree-and-person-required")
        page = inner.get(f"{_BASE}/family-tree/person/tree/{tree_id}/person/{person_id}/facts", timeout=30,
                         allow_redirects=False, writes_ok=False)
        values["userId"] = actor_from_page(page.text, config.account_id())
    for key in re.findall(r"\{(\w+)\}", path):
        if key not in values:
            raise LaneError("missing-path-param", missing=key)
        path = path.replace("{" + key + "}", values[key])
    return path


def _call(endpoint, values, params, full, tree_id, person_id, bridge):
    with rt.lock("ancestry"):
        lease = rt.open_lane(bridge)
        inner = bridge.ChromeBrowserSession(cdp_url=lease.base, target_id=lease.target_id)
        path = _fill_path(endpoint, values, inner, tree_id, person_id)
        lease.check()
        resp = inner.get(_BASE + path, params=params or None, timeout=60, allow_redirects=False, writes_ok=False)
    text = resp.text if isinstance(resp.text, str) else ""
    status = getattr(resp, "status_code", None)
    try:
        body = json.loads(text)
        out = {"body": body} if full else {"shape": shape(body)}
    except ValueError:
        out = {"text": text[:200_000]} if full else {"text_length": len(text)}
    ok = isinstance(status, int) and 200 <= status < 300
    code = None if ok else classify_preflight(status, text)
    if code == "bot-challenge":
        trip_breaker()
    # a 404 on a read is an ordinary "not found" for the caller, not a person-binding error
    classification = "read" if ok else ("read-failed" if code in (None, "person-not-in-tree", "preflight-failed") else code)
    return {"ok": ok, "classification": classification, "dispatch_attempted": True, "status": status, **out}


def read(*, action, name=None, tree_id=None, person_id=None, path_params=None, params=None, full=False, bridge=None,
         given=None, surname=None, birth=None, death=None, location=None, collection=None, record_id=None, counts=False):
    if action in ("search", "hints", "record"):
        plan = _named(action, given=given, surname=surname, birth=birth, death=death, location=location,
                      collection=collection, record_id=record_id, counts=counts, tree_id=tree_id, person_id=person_id)
        if plan is None:
            return failure("missing-arguments")
        name, path_params, params = plan
        action = "get"
    if tree_id is not None and not config.tree_allowed(tree_id):
        return failure("configuration-error")
    bridge = bridge or rt.load_bridge()
    if action == "list":
        rows = [{k: e[k] for k in _LIST_FIELDS if e.get(k)} for e in _endpoints(bridge) if name in (None, e["group"], e["name"])]
        return {"ok": True, "classification": "endpoints", "count": len(rows), "endpoints": rows}
    if name in _SIDE_EFFECT_READS:
        return failure("side-effecting-read")
    endpoint = next((e for e in _endpoints(bridge) if e["name"] == name), None)
    if endpoint is None:
        from .runtime import suggest
        return failure("unknown-endpoint", problems=[{"field": "name", "issue": "unknown-value",
                                                      "did_you_mean": suggest(name, [e["name"] for e in _endpoints(bridge)])}])
    values = {k: str(v) for k, v in (path_params or {}).items()}
    if tree_id is not None:
        values.setdefault("treeId", str(tree_id))
    if person_id is not None:
        values.setdefault("personId", str(person_id))
    try:
        with rt.quiet():
            return _call(endpoint, values, params, full, tree_id, person_id, bridge)
    except LaneError as exc:
        return failure(exc.code, **exc.detail)
    except Exception as exc:
        return failure(classify_exception(exc) or "native-operation-failed")
