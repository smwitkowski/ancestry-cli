"""Merge-job payload builders, vendored verbatim from an earlier prototype (pure functions; they only read the
comparison response). Source: ancestry_tools._build_upload_body / _build_confirmation_body."""
from __future__ import annotations


def _build_upload_body(comparison: dict, *, hint_id: str | None, person_gid: str, source_gid: str,
                       cite_only: bool) -> dict:
    """Transform comparison response → upload POST body.

    The comparison response holds the merge UI's pre-bootstrap state (existing
    tree facts side-by-side with the record's facts). The upload body is what
    the UI POSTs when the user clicks Save with the default checkbox state.

    Default behavior (cite_only=False, matches the UI's default):
      - For each event with a tree-side match: cite source on the existing
        assertion (no fact change).
      - For each record-only event (tree has no matching fact): add as a new
        event citing the record.
      - Name is cited on the existing tree name (record-side surname is NOT
        added as an alt name — that requires explicit user opt-in in the UI).

    cite_only=True: skip all record-only events (citation only, no new facts).
    """
    record_node = comparison.get("RecordNodes", {}).get("1:99") or {}
    if not record_node:
        raise RuntimeError("comparison response missing RecordNodes['1:99']")

    # --- Names: cite on existing tree name (skip record-only alt surnames by default) ---
    names: list[dict] = []
    name_obj = record_node.get("Name") or {}
    tree_name = (name_obj.get("Tree") or {})
    if tree_name.get("AssertionId"):
        names.append({
            "Given": tree_name.get("Given", "") or "",
            "Surname": tree_name.get("Surname", "") or "",
            "Suffix": tree_name.get("Suffix", "") or "",
            "AssertionId": tree_name["AssertionId"],
            "CiteSource": True,
        })

    # --- Events: cite matched events; add record-only events unless cite_only ---
    events: list[dict] = []
    for ev in record_node.get("Events", []) or []:
        tree_side = ev.get("Tree") or {}
        record_side = ev.get("Record") or {}
        ev_type = ev.get("Type", "")
        if tree_side.get("AssertionId"):
            # Cite source on existing tree fact (don't change date/place)
            events.append({
                "AssertionId": tree_side["AssertionId"],
                "Type": ev_type,
                "CiteSource": True,
            })
        elif record_side and not cite_only:
            # New event from the record
            new_ev = {"Type": ev_type, "CiteSource": True}
            if record_side.get("Date"):
                new_ev["Date"] = record_side["Date"]
            if record_side.get("Place"):
                new_ev["Place"] = record_side["Place"]
            events.append(new_ev)

    # --- Family: copy from record_node (record's own family graph; usually empty for vital indexes) ---
    family = record_node.get("Family") or {"Father": "", "Mother": "", "Siblings": [], "FamilyUnits": []}

    node_payload = {
        "EvidencePointers": [person_gid, source_gid],
        "Events": events,
        "Names": names,
        "Family": family,
    }
    if hint_id:
        node_payload["HintId"] = hint_id
        node_payload["HintType"] = "record"

    # --- Image: only include if the record has one ---
    image_data = comparison.get("imageData") or {}
    if image_data.get("RecordImageId"):
        node_payload["Image"] = {
            "RecordDbId": comparison.get("RecordDatabaseId", ""),
            "RecordId": comparison.get("RecordId", ""),
            "RecordImageId": image_data["RecordImageId"],
        }

    # --- TreeRelationshipsMap: flatten TreeNodes → {nodeId: {parentId: ['F'|'M'], spouseId: ['H'|'W']}} ---
    tree_rel_map: dict = {}
    for node_id, node in (comparison.get("TreeNodes") or {}).items():
        rels: dict = {}
        fam = node.get("Family") or {}
        if fam.get("Father"):
            rels[fam["Father"]] = ["F"]
        if fam.get("Mother"):
            rels[fam["Mother"]] = ["M"]
        for unit in fam.get("FamilyUnits") or []:
            if unit.get("Husband"):
                rels[unit["Husband"]] = ["H"]
            if unit.get("Wife"):
                rels[unit["Wife"]] = ["W"]
        if rels:
            tree_rel_map[node_id] = rels

    # --- Citations: copy comparison Citations, augment with title from Sources[0].title if missing ---
    citations = list(comparison.get("Citations") or [])
    sources = comparison.get("Sources") or []
    if citations and sources and "title" not in citations[0]:
        citations[0] = {**citations[0], "title": sources[0].get("title", "")}

    return {
        "Nodes": {"1:99": node_payload},
        "Citations": citations,
        "Sources": sources,
        "Repositories": comparison.get("Repositories") or [],
        "TreeRelationshipsMap": tree_rel_map,
        "IsSourcePm3": False,
    }


def _build_confirmation_body(*, hint_id: str | None, person_gid: str, source_gid: str) -> dict:
    """Build the post-upload confirmation body."""
    accept_entry = {"PersonGid": person_gid, "SourceGid": source_gid}
    if hint_id:
        accept_entry["HintId"] = hint_id
    return {"postConfirmationPayload": {"merge": {"hintsToAccept": [accept_entry]}}}
