# Instructions for agents

This repository is `ancestry`, a command-line tool that reads and edits an Ancestry.com family tree through the owner's own
signed-in Chrome. You will most likely **use** it (section 1); you may also be asked to **change** it (section 2).

## 1. Using the tool

Start here, in this order:

1. `ancestry doctor`: confirms Chrome, sign-in, the pinned account and the configured trees. If it fails, read `next_actions`; a
   `human` step means stop and ask the owner. Do not try to sign in, solve challenges or edit `~/.ancestry-cli/config.json` yourself.
2. `ancestry trees`: the trees you can use (aliases and which are writable).
3. `ancestry ops`: every write operation with its risk class; `ancestry ops <op>` gives required fields and an example.
4. Read [docs/agents.md](docs/agents.md) for conventions and a research loop.

Rules that matter most:

- **Dry-run first.** Writes only happen with `--live`. Dry-run, check the shape, then run live.
- **Never retry a write whose `state` is `unknown`** (exit code 3). Run `ancestry journal verify --id <journal_id>`, decide, then
  `ancestry journal resolve --id <journal_id> --as ok|failed`. Do not use `--force` to get past `duplicate-write` unless verify shows
  the earlier write did not happen.
- **Follow `next_actions`**; fix every item in `problems` at once; respect `retry_after`; at most 2 retries per command, then stop.
- **`relative-add` needs `status`.** If you do not know whether a person is living, say `Living`.
- **People who may be living are redacted** in `find` and `person`. Do not use `--include-living` unless the task needs it.
- **Ask the owner before** removing a person or fact, accepting a hint, uploading media, or any write on a tree that is not a sandbox
  tree. Use a sandbox tree to practice. Everything is journaled: `ancestry journal list`, `ancestry journal undo --id N --live`.
- The tool does not judge whether a record matches a person. Apply your own evidence standard and cite sources.

Never paste a tree's private data into anything outside the conversation, and never commit `~/.ancestry-cli/` (it holds the journal and
snapshots of the owner's tree).

## 2. Changing the tool

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md). Short version:

- `uv sync && uv run pytest` (offline; a fake browser stands in for Chrome). Lint: `uvx ruff check src tests scripts --select F,E9,B,UP,SIM,PLE --ignore E501,B011 --exclude src/ancestry_cli/vendor`.
- Operations live in one registry, `src/ancestry_cli/ops.py`. New endpoints must be captured from the real web app first and added to
  `src/ancestry_cli/vendor/api_endpoints.py`; the bridge refuses anything not listed there.
- `docs/operations.md` and `docs/errors.md` are generated: run `uv run python scripts/gen_docs.py` (a test fails if they are stale).
- New failure modes need a row in `runtime.ERRORS` with `retryable`, `needs_human`, a hint and `next_actions`.
- Never put real tree data (ids, names, places) in code, tests or docs; use placeholders. Never store names or free text in the journal.
- After changing code, reinstall the command: `uv tool install --force --reinstall .`
