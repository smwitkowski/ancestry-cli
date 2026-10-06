"""Record collections (read-only): where a person or keyword has hits, what a collection searches on, and a local catalogue.

Ancestry has no public list of every collection. `hit_counts` returns, per category, the top five collections for a query, so
`find` is the discovery tool; every collection it sees is remembered in `~/.ancestry-cli/collections.json` for `list`."""
from __future__ import annotations

import json

from . import config, reads
from .runtime import failure

_CATALOGUE = "collections.json"


def _load():
    try:
        return json.loads((config.HOME / _CATALOGUE).read_text())
    except (OSError, ValueError):
        return {}


def _remember(rows):
    cat = _load()
    for r in rows:
        cat[str(r["collection_id"])] = {"label": r["label"], "category": r["category"], "category_id": r["category_id"]}
    config.HOME.mkdir(parents=True, exist_ok=True)
    (config.HOME / _CATALOGUE).write_text(json.dumps(cat, indent=0, sort_keys=True) + "\n")


def find(*, given=None, surname=None, birth=None, death=None, location=None, keyword=None, category=None, bridge=None):
    """Collections with hits for a person and/or keyword (top five per category), most hits first."""
    if not (surname or keyword):
        return failure("missing-arguments")
    params = {"priority": "usa", "searchMode": "advanced"}
    if surname:
        params["name"] = f"{(given or '').replace(' ', '+')}_{surname.replace(' ', '+')}"
    params.update({k: v for k, v in (("birth", birth), ("death", death), ("location", location), ("keyword", keyword),
                                      ("category", category)) if v})
    res = reads.read(action="get", name="hit_counts", params=params, full=True, bridge=bridge)
    if not res.get("ok"):
        return res
    seen = {}
    for cat in res["body"].get("results", {}).get("items", []):
        cid = str(cat.get("Token", "")).rpartition("=")[2]
        for col in cat.get("Collections", []):
            _, _, ident = col["Token"].rpartition("COL=")
            row = seen.setdefault(ident, {"collection_id": int(ident), "label": col["Label"], "hits": col["Count"],
                                          "category": cat["Label"], "category_id": cid, "also_in": []})
            if row["category"] != cat["Label"]:
                row["also_in"].append(cat["Label"])
    rows = sorted(seen.values(), key=lambda r: -r["hits"])
    _remember(rows)
    return {"ok": True, "classification": "collections", "count": len(rows), "collections": rows,
            "note": "Top five collections per category only. Narrow with --category, --keyword or --location to see others."}


def describe(collection_id, *, bridge=None):
    """What a collection is, how it is browsed, and which search fields it accepts."""
    out = {"ok": True, "classification": "collection", "collection_id": collection_id}
    for key, endpoint in (("browse", "browse_collection"), ("fields", "facet_fields_collection")):
        res = reads.read(action="get", name=endpoint, path_params={"collectionId": collection_id}, full=True, bridge=bridge)
        if not res.get("ok"):
            return res
        out[key] = res["body"]
    out["label"] = _load().get(str(collection_id), {}).get("label")
    out["search_with"] = f"ancestry read search --surname S --collection {collection_id}"
    return out


def listing(*, query=None):
    """The local catalogue: every collection seen so far, optionally filtered by words in its title."""
    words = (query or "").lower().split()
    rows = [{"collection_id": int(k), **v} for k, v in sorted(_load().items(), key=lambda kv: kv[1]["label"])
            if all(w in v["label"].lower() for w in words)]
    return {"ok": True, "classification": "collections", "count": len(rows), "collections": rows,
            "note": "Only collections seen in earlier `collections find` runs. Run `find --keyword WORD` to discover more."}
