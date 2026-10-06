"""Who am I, which trees do I have, who is this person: the commands that turn names into ids (read-only)."""
from __future__ import annotations

import datetime

from . import config
from .runtime import get_json, guarded, session
from .snapshots import gpid

_YEAR = datetime.date.today().year
_REDACTED = "Living person (redacted)"


def _own_and_shared(inner):
    rows = []
    for rights, role in (("own", "own"), ("shared", "shared")):
        data = get_json(inner, "/api/treesui-list/trees", {"rights": rights, "page": "0", "limit": "100"})
        rows += [{**t, "_role": role} for t in data.get("trees", [])]
    return rows


def whoami(*, pin=False):
    def run():
        with session() as (_b, _l, inner):
            owners = sorted({t.get("ownerUserId") for t in _own_and_shared(inner) if t["_role"] == "own" and t.get("ownerUserId")})
        if len(owners) != 1:
            return {"ok": False, "classification": "account-unresolved", "dispatch_attempted": False,
                    "hint": "Could not tell the account from your owned trees."}
        guid, pinned = owners[0], config.account_id()
        if pin:
            config.pin_account(guid)
            pinned = guid
        return {"ok": True, "classification": "whoami", "account_id": guid, "profile": config.profile_name(),
                "port": config.port(), "pinned": pinned, "matches_pin": (pinned == guid) if pinned else None}
    return guarded(run)


def trees():
    def run():
        with session() as (_b, _l, inner):
            rows = _own_and_shared(inner)
        by_id = {v: k for k, v in config.aliases().items()}
        out = []
        for t in rows:
            tid = int(t["id"])
            out.append({"tree_id": str(tid), "name": t.get("name"), "role": t["_role"], "modified": t.get("dateModified"),
                        "alias": by_id.get(tid), "sandbox": tid in config.sandbox_trees(), "writable": config.writable(tid)})
        return {"ok": True, "classification": "trees", "trees": out}
    return guarded(run)


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _possibly_living(birth, death):
    return death is None and birth is not None and birth > _YEAR - 100


def _score(row, given, surname, birth, death):
    g, s = (row.get("GivenName") or "").lower(), (row.get("Surname") or "").lower()
    score = 0
    if given:
        score += 2 if g == given.lower() else 1 if g.startswith(given.lower()) else 0
    if surname:
        score += 2 if s == surname.lower() else 1 if s.startswith(surname.lower()) else 0
    for want, have in ((birth, _num(row.get("BirthYear"))), (death, _num(row.get("DeathYear")))):
        if want is not None and have is not None:
            score += 2 if want == have else 1 if abs(want - have) <= 2 else -1
    return score


def find(*, tree_id, given=None, surname=None, birth=None, death=None, limit=20, include_living=False):
    if not (given or surname):
        return {"ok": False, "classification": "usage-error", "dispatch_attempted": False}

    def run():
        with session() as (_b, _l, inner):
            rows = get_json(inner, f"/api/person-picker/suggest/{tree_id}",
                            {"partialFirstName": given or "", "partialLastName": surname or "", "isHideVeiledRecords": "true"})
        if not isinstance(rows, list):
            rows = []
        scored = sorted(((_score(r, given, surname, birth, death), r) for r in rows), key=lambda x: -x[0])
        out = []
        for score, r in scored[:limit]:
            b, d = _num(r.get("BirthYear")), _num(r.get("DeathYear"))
            living = _possibly_living(b, d)
            hide = living and not include_living
            out.append({"tree_id": str(tree_id), "person_id": str(r.get("PersonId")), "score": score,
                        "name": _REDACTED if hide else r.get("FullName"),
                        "given": None if hide else r.get("GivenName"), "surname": None if hide else r.get("Surname"),
                        "birth_year": None if hide else b, "death_year": None if hide else d, "possibly_living": living})
        return {"ok": True, "classification": "find", "results": out, "total": len(rows), "truncated": len(rows) > limit}
    return guarded(run)


def person(*, tree_id, person_id, include_living=False):
    def run():
        from .snapshots import person_data, snapshot_from_page
        with session() as (_b, _l, inner):
            resp = inner.get(f"https://www.ancestry.com/family-tree/person/tree/{tree_id}/person/{person_id}/facts",
                             timeout=60, allow_redirects=False, writes_ok=False)
            from .runtime import LaneError, classify_preflight
            code = classify_preflight(getattr(resp, "status_code", None), resp.text)
            if code:
                raise LaneError(code)
        pr = person_data(resp.text)["person"]["PersonResearch"]
        snap = snapshot_from_page(resp.text, tree_id, person_id)
        living = bool(pr.get("IsPersonLiving"))
        base = {"ok": True, "classification": "person", "tree_id": str(tree_id), "person_id": str(person_id),
                "living": living, "can_edit": pr.get("HasTreeEditRights")}
        if living and not include_living:
            return {**base, "name": _REDACTED, "redacted": True}
        family = {}
        for role in ("Fathers", "Mothers", "Spouses", "Siblings", "Children"):
            members = (pr.get("PersonFamily") or {}).get(role) or []
            family[role.lower()] = [{"person_id": str(m.get("Id")), "name": _REDACTED if m.get("IsLiving") and not include_living else m.get("FullName"),
                                     "life_range": None if m.get("IsLiving") and not include_living else m.get("LifeRange")}
                                    for m in members if isinstance(m, dict)]
        facts = [{"assertion_id": str(f["AssertionId"]), "type": f.get("TypeString"), "date": f.get("Date"), "place": f.get("Place"),
                  "place_id": gpid(f.get("PlaceGpids")) or None, "preferred": not f.get("IsAlternate"),
                  "description": f.get("Description"), "source_count": len(str(f.get("SourceCitationIDs") or "").split())}
                 for f in snap["facts"] if f.get("AssertionId")]
        return {**base, "name": snap["name"], "facts": facts, "sources": len(snap["sources"]),
                "citations": [{"citation_id": str(s["CitationId"]), "source_id": s.get("SourceId"), "custom": s.get("custom", False),
                               "title": s.get("Title")} for s in snap["sources"] if s.get("CitationId")], "family": family}
    return guarded(run)
