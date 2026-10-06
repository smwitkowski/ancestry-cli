"""Sources in a tree (read-only): find existing ones by title so they are reused instead of duplicated."""
from __future__ import annotations

from . import config, reads
from .runtime import failure

_PAGE = 100


def current(inner, actor, tree_id, source_id):
    """One source's row from the tree's source list, or None. `inner` is a bridge session."""
    import json
    path = f"/family-tree/person/sourceedit/user/{actor}/tree/{tree_id}/sources/"
    for page in range(1, 51):
        got = inner.request("GET", "https://www.ancestry.com" + path, params={"page": str(page), "limit": str(_PAGE), "sort": "title"},
                            timeout=30, allow_redirects=False,
                            _validated_endpoint={"method": "GET", "path": path, "side_effect": False})
        rows = json.loads(got.text)
        for row in rows:
            if str(row.get("sourceId")) == str(source_id):
                return row
        if len(rows) < _PAGE:
            return None
    return None


def find(*, tree_id, person_id, query=None, bridge=None):
    """List the tree's custom sources, optionally only those whose title has every word of `query`."""
    if tree_id is None or not config.tree_allowed(tree_id) or (person_id is None and not config.account_id()):
        return failure("configuration-error")        # without --person the pinned account id supplies the user id
    words = (query or "").lower().split()
    rows, page = [], 1
    while page <= 50:
        res = reads.read(action="get", name="sources_list", tree_id=tree_id, person_id=person_id,
                         path_params={"userId": config.account_id()} if config.account_id() else None,
                         params={"page": str(page), "limit": str(_PAGE), "sort": "title"}, full=True, bridge=bridge)
        if not res.get("ok"):
            return res
        batch = res["body"]
        rows += [r for r in batch if all(w in str(r.get("title", "")).lower() for w in words)]
        if len(batch) < _PAGE:
            break
        page += 1
    keep = ("sourceId", "title", "auth", "pub", "publ", "pubd", "cn", "refn")
    out = [{**{k: r[k] for k in keep if r.get(k)}, "repository_id": str((r.get("rgid") or {}).get("v", "")).split(":")[0] or None}
           for r in rows]
    return {"ok": True, "classification": "sources", "count": len(out), "sources": out}
