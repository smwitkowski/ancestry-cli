# Command reference

Every command prints **one JSON object** on stdout. Exit codes: `0` ok, `1` the operation failed with nothing changed, `2` bad usage,
`3` a request was sent and its result is unknown (stop and run `ancestry journal verify`). A failing result always has a `classification`
and a `state`; known failures also carry `retryable`, `needs_human`, a `hint` and machine-readable `next_actions`
([errors.md](errors.md)). A global `--profile NAME` selects a config profile ([config](#config-file)).

Tree arguments accept a numeric id or an alias from `config.json`. Person ids are the number in a person-page URL.

## Setup and identity

### `ancestry doctor`
Checks, in order: the bot-challenge block, unresolved unknown outcomes, the browser lane (exactly one tab on
ancestry.com), an in-page request, sign-in, the pinned account, and that configured `sandbox_trees` / `write_trees`
are trees you own. Result: `{"ok", "checks": {name: "ok" | reason}, "home", "profile", "port", ...}`. Run it first, and
whenever something fails unexpectedly.

### `ancestry whoami [--pin]`
The signed-in account id, read from your owned trees. `--pin` saves it as `account_id` in the active profile; from then on
every write refuses to run if the page shows a different account.

### `ancestry trees`
Owned and shared trees: `[{tree_id, name, role: own|shared, modified, alias, sandbox, writable}]`. `writable` reflects
your `write_trees` allowlist, not the server's rights.

### `ancestry lane reset`
Navigates the single tab to a known-good page. Use after `lane-page-unusable` (an error page). Needs exactly one tab.

## Finding people (read-only)

### `ancestry find --tree T [--given G] [--surname S] [--birth Y] [--death Y] [--limit N] [--include-living]`
Candidates ranked by name and year fit: `{results: [{tree_id, person_id, name, given, surname, birth_year, death_year,
score, possibly_living}], total, truncated}`. People who may be living (no death year, born in the last 100 years) are
redacted unless `--include-living`. At least one of `--given` / `--surname` is required.

### `ancestry person --tree T --person P [--include-living]`
One person: `{name, living, can_edit, facts: [{assertion_id, type, date, place, description, source_count}], sources,
family: {fathers, mothers, spouses, siblings, children}}`. Use the `assertion_id`s with `fact-edit` and friends.
A living person is returned redacted unless `--include-living`.

## Reading records (read-only)

### `ancestry read search --given G --surname S [--birth Y] [--death Y] [--location L] [--collection N] [--counts] [--full]`
Record search. `--counts` returns hit counts per category; `--collection N` searches inside one collection. By default only
the response's shape is returned; `--full` returns the data (`results.items[*].collectionId/recordId/imageIds`).
### `ancestry sources --tree T [--person P] [--query WORDS]`
Lists the tree's custom sources (id, title, author, publisher, call number, repository id), optionally only titles containing every
word. Run it before `write source-create` so a source is reused, not duplicated. `--person` is optional once the account is pinned.
Source and citation fields: `write source-create` and `source-edit` take author, publisher, publication_place, publication_date,
call_number, refn, note and repository_id; `write repository-create` makes a repository and returns its id;
`write source-set-repository` links one; `write citation-add` and `citation-edit` take date, other_info, transcription and url.
`citation-edit` replaces every citation field (omitted ones are cleared); `source-edit` keeps fields you do not give.

### `ancestry apply --manifest FILE [--live]`
Runs a batch of writes from a JSON file: `{"tree": "alias or id", "confirm_tree": id (outside sandbox trees), "operations":
[{"op": "fact-add", "person": 111, "assertion": 222, "set": {"eventType": "Birth", "date": "1900"}}]}`. Without `--live` every step is
validated and nothing is sent. With `--live` the first step is the canary: if it is rejected or changes nothing the batch stops.
Later steps stop at the first failure or unknown outcome (exit 3: do not retry; `journal verify`). Each result (ok, state,
journal_id, ids, readback) is appended to the manifest's `receipt`; re-running skips steps already recorded as ok.

### `ancestry collections find|describe|list`
Record collections. Ancestry publishes no full list, so this has three parts:
- `find [--given G] --surname S [--birth Y] [--location L] [--keyword WORD] [--category C]`: collections with hits for a person
  and/or keyword (id, title, hits, category). Ancestry returns only the **top five per category**; narrow with `--category`
  (for example 36, Court, Land, Wills & Financial), `--keyword deed` or `--location` to see others. Every collection seen is saved to
  `~/.ancestry-cli/collections.json`.
- `describe ID`: how the collection is browsed and which search fields it accepts.
- `list [--query WORDS]`: the saved catalogue, filtered by words in the title.
Then search inside one: `ancestry read search --surname S --collection ID`.

### `ancestry read hints --tree T --person P`, `ancestry read record --collection C --record R`
Hints page data and a record's details. **Take record and collection ids from a live search result**, not from examples.
### `ancestry read list [GROUP]` / `ancestry read get NAME [--tree T --person P --path k=v --param k=v] [--full]`
Any known read endpoint (about 60: trees, people, search metadata, images, media). `read list` shows names, paths and
query parameters.

## Hints

### `ancestry hint list --tree T --person P`
`[{hint_id, record_id, collection_id}]` for the person's new hints (`record_id` is null for non-record hints).

### `ancestry hint accept --tree T --person P --hint-id H [--cite-only] [--live] [--confirm-tree T] [--force]`
Accepts a record hint the way the web app does: new facts from the record are added, existing facts are cited, no
alternate names. The dry-run reports counts only: `{preview: {events_cited_existing, events_new_from_record,
names_cited}}`. The accept is journaled under `manual_cleanup`; reverse it with `fact-detach-source`, `fact-remove` and
`citation-remove`, then `write hint-new` to put the hint back to New.

## Writing

### `ancestry write OP --tree T --person P [--assertion A] --set key=value ... [--live] [--confirm-tree T] [--force]`
See [operations.md](operations.md) for every operation. A live result:
`{ok, classification: "sent", status, ids, response_shape, snapshot?, readback?}` where `ids` holds what the write created
(`newPid`, `AttributeIds`, `gid`, `webLinkId`, `mediaId`, ...), `snapshot` is the before-state file name (edits and removals)
and `readback` lists what changed (ids and field names, never values).
`ancestry ops [OP]` prints the same operation descriptions as JSON.

Guards, in the order they apply to a live write: the tree must be in `write_trees` (if configured), repeat `--confirm-tree`
unless the tree is a sandbox tree, an identical write in the last 24 hours is refused unless `--force`, then the page
preflight (status, edit rights, pinned account) and, for edits and removals, the before-snapshot.

## Journal

### `ancestry journal list`
`{outstanding: [...], manual_cleanup: [...]}`. `outstanding` are changes with a known inverse; `manual_cleanup` are things that
cannot be undone automatically (custom sources, accepted hints) or writes with an unknown outcome.
### `ancestry journal undo (--id N | --all) [--live]`
Runs the inverse of one entry (or all, newest first). Dry-run unless `--live`. `--all` skips trees outside your sandbox trees:
undo those one at a time with `--id`.
### `ancestry journal verify --id N`
Reads the person's current state so you can see whether a write with an unknown outcome actually happened.
### `ancestry journal resolve --id N --as ok|failed`
Records your decision, which clears the entry from `manual_cleanup`.

## Config file
`~/.ancestry-cli/config.json` (override the folder with `ANCESTRY_CLI_HOME`):

```json
{"default_profile": "main",
 "profiles": {"main": {"port": 9224, "account_id": "<from whoami --pin>",
                       "trees": {"main": 123, "sandbox": 456},
                       "write_trees": [123, 456], "sandbox_trees": [456]}}}
```
A flat file with the same keys at the top level is the `default` profile. Environment: `ANCESTRY_CLI_HOME`, `ANCESTRY_CLI_PORT`,
`ANCESTRY_CLI_PROFILE`, `ANCESTRY_CLI_JOURNAL`, `ANCESTRY_CLI_SNAPSHOTS`, `ANCESTRY_CLI_LOCK_FILE` (one lock file shared with other
tools on the same account; its content is the last call's epoch seconds, so any flock-based pacer using that format interoperates).
