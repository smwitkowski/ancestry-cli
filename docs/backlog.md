# Backlog

- **Collection recommender.** Given a person (birth and death years, places, relatives), suggest which collections to search
  and in what order. Start from `collections find` hit counts, then weight by place and date overlap with each collection's
  title and browse hierarchy. Needs a bigger catalogue than the five-per-category the API returns: seed it by running
  `collections find` over many keywords and places.
- Collection browse by place (`browse_collection_hierarchy` already works: state, then county, then city).
- See also the open gaps in [architecture.md](architecture.md).

## From the first multi-agent setup (reviewer notes)
- Default `write_trees` to deny (today a missing list means every owned tree is writable; `doctor` now warns).
- Read-back and snapshots do not carry preferred flags, place ids (GPID) or name parts, so the verified diff cannot prove they were preserved.
- Dry-run on a non-writable tree shows a partial request body; load the live form or label it partial.
- `journal undo --live` returns no `state` or read-back; writes do.
- The lane needs exactly one page per Chrome; bind to a tab by origin instead so other sites can share the browser.
- `find` uses the person picker and may not be exhaustive; document it and add a complete in-tree search.
- `source-delete` cannot yet warn with the citation count (no route known that lists a source's citations).
- Add an actor label (for example `ANCESTRY_CLI_ACTOR`) to journal entries when homes are shared.
