# Using ancestry from an agent

The tool is built so that an agent can drive it by reading JSON, without a human translating. Rules of the road:

## Conventions

- **One JSON object per command**, always. Parse stdout. Exit codes: `0` ok, `1` failed with nothing changed, `2` bad usage,
  **`3` a request was sent and its result is unknown: stop and verify before doing anything else.**
- **Look before you write.** `ancestry ops` lists the operations; `ancestry ops fact-edit` gives required fields, risk class,
  undo behavior and an example. `ancestry person` gives the fact ids you need.
- **Dry-run first.** Every write is a dry-run unless `--live`. A dry-run validates all the fields and shows the request shape.
- **Trust `state`** (on every failure and write): `unchanged` = nothing was changed, `unknown` = a request left and the result is
  not known, `changed` = confirmed. Never assume more than it says.
- **Follow `next_actions`.** A failing result carries an ordered list of recovery steps:
  `{"kind": "run", "argv": [...]}` (execute exactly that), `{"kind": "retry"}` (re-run the same command), `{"kind": "human", "say": ...}`
  (only the owner can do this: stop and ask), `{"kind": "edit-args", "say": ...}` (fix your arguments). `when` says when a step applies;
  `<name>` in an argv marks a choice you make. `retry_after` (seconds) says how long to wait before a retry.
- **Fix every entry in `problems` at once** (validation errors): `{field, issue: missing|invalid|not-allowed|unknown-value, valid_values?,
  did_you_mean?}`. The values are never echoed back.
- **Never retry a write whose `state` is `unknown`.** Use the `journal_id` in the result: `ancestry journal verify --id N`, decide whether
  the change happened, then `ancestry journal resolve --id N --as ok|failed`. A blind retry is also stopped by the duplicate guard.
- **Retry budget:** at most 2 retries of the same command for a `retryable` code; if it keeps failing, run `ancestry doctor` and stop.
- **Ids are strings** in discovery output. Pass them back as given.
- **Warnings** (`warnings: [{code, message}]`) can appear on successful results: read them.

## A research loop

```bash
ancestry doctor                                        # lane, sign-in, account, trees
ancestry trees                                         # which tree (use the alias)
ancestry find --tree main --given Mary --surname Smith --birth 1850
ancestry person --tree main --person 123456            # facts with ids, sources, family
ancestry read search --given Mary --surname Smith --birth 1850 --full     # candidate records (take ids from this output)
ancestry hint list --tree main --person 123456
ancestry hint accept --tree main --person 123456 --hint-id 99 --cite-only        # dry-run: counts only
ancestry write note-set --tree main --person 123456 --set "text=Census 1880 fits; needs a birth record"
```

## Choosing what to run

| you want to | use |
|---|---|
| turn a name into ids | `find`, then `person` to confirm |
| see what a person has | `person` |
| look for records | `read search` (`--counts` first to see which categories have hits) |
| act on a hint | `hint list`, `hint accept --cite-only` (cites the record without adding facts), or `write hint-no` / `hint-maybe` / `hint-ignore` |
| record a finding | `write note-set` (shared note), `write fact-add` + `source-create` + `citation-add` + `fact-attach-source` |
| fix a fact | `write fact-edit --assertion A --set field=value` (only the fields you give change) |
| back something out | `journal list`, `journal undo --id N --live` |

## Safety rules the tool enforces (so you do not have to remember them)

- Live writes only touch trees in `write_trees`, need `--confirm-tree` outside sandbox trees, and refuse an identical repeat
  within 24 hours unless `--force`. Do not pass `--force` to get past a `duplicate-write` unless you have checked with
  `journal verify` that the earlier write did not happen.
- `relative-add` always needs `status=Living|Deceased`. If you do not know, treat the person as **Living**: it is the safe choice.
- People who may be living are redacted in `find` and `person`; do not pass `--include-living` unless the task requires it.
- Edits and removals are snapshotted and can be undone with `journal undo --id N`; removing a person cannot be undone.
- Custom sources cannot be deleted. Reuse an existing source instead of creating a new one for each citation.

## What the tool does not do

It does not decide whether a record matches a person, and it does not apply your evidence standards: that is the agent's job.
It does not merge people, edit tree settings, upload GEDCOMs, or link an *existing* tree person as a relative.
