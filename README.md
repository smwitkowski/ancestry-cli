# ancestry-cli

Read and edit an Ancestry.com family tree from the command line, using **your own signed-in Chrome**. It never sees your
password or cookies, prints one JSON object per command, snapshots what it changes, journals every write so it can be
undone, and refuses risky writes unless you say so explicitly. Built to be used by people and by AI agents.

Status: 0.1.0, a working tool built from captured web-app traffic. It is not an official Ancestry API and can break if
Ancestry changes its site.

## Requirements

- macOS or Linux (the cross-process lock uses `fcntl`; Windows is not supported).
- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or pipx).
- Google Chrome, and an Ancestry.com account you can sign in to.

## Install

```bash
git clone <this repository> ancestry-cli && cd ancestry-cli
uv tool install .                       # puts `ancestry` on your PATH (or: pipx install .)
uv tool install --force --reinstall .   # after pulling or changing the code
```

## One-time setup

Run one command:

```bash
ancestry init
```

It opens a dedicated Chrome (its own profile folder, remote debugging on port 9224) at the Ancestry sign-in page. **You sign in
by hand**; the tool never sees or types your password. When you are signed in it pins your account and finishes. After that,
leave that one tab open on www.ancestry.com (the tool never opens, navigates or closes tabs) and run `ancestry doctor`.

Why a browser at all? Ancestry blocks scripted logins, so the tool borrows a browser you signed in to yourself. Every request
runs inside that tab, so your cookies never reach the tool. The debugging port gives local programs control of that Chrome, so
use a machine and account you trust. To set it up manually instead, start Chrome with
`--remote-debugging-port=9224 --user-data-dir="$HOME/.ancestry-cli/chrome-profile"`.

Optionally create `~/.ancestry-cli/config.json` with tree aliases and a `write_trees` allowlist ([commands.md](docs/commands.md#config-file)).

## Quick start

```bash
ancestry trees                                                  # your trees
ancestry find --tree main --given Mary --surname Smith --birth 1850
ancestry person --tree main --person 123456                     # facts (with ids), sources, family
ancestry read search --given Mary --surname Smith --birth 1850  # records
ancestry write note-set --tree main --person 123456 --set "text=Needs a birth record"          # dry-run
ancestry write note-set --tree main --person 123456 --set "text=Needs a birth record" --confirm-tree main --live
ancestry journal list                                           # what was changed, and how to undo it
```

## For an AI agent

Read [AGENTS.md](AGENTS.md) first (five minutes), then [docs/agents.md](docs/agents.md). Run `ancestry doctor`, then `ancestry ops` to
see what can be written. Every command prints one JSON object; follow `state` and `next_actions` on failures.

## Documentation

| | |
|---|---|
| [docs/commands.md](docs/commands.md) | every command, its flags and output; the config file |
| [docs/operations.md](docs/operations.md) | every write operation: fields, risk, undo, example (generated) |
| [docs/errors.md](docs/errors.md) | every error classification and what to do (generated) |
| [docs/agents.md](docs/agents.md) | using the tool from an AI agent |
| [docs/architecture.md](docs/architecture.md) | how it works and what it guarantees |
| [docs/error-roadmap.md](docs/error-roadmap.md) | how errors are designed for agents, and what was built |
| [CONTRIBUTING.md](CONTRIBUTING.md) | adding an operation, running tests |

## Commands at a glance

`doctor` · `whoami [--pin]` · `trees` · `find` · `person` · `read` (search, hints, record, any known read) · `collections` (find, describe, list) · `sources` · `match` · `apply` · `hint list|accept` ·
`write OP` (28 operations, `ancestry ops` describes them) · `journal list|undo|verify|resolve` · `lane reset`.
Details and flags: [docs/commands.md](docs/commands.md).

## Safety in one paragraph

Writes are dry-run unless `--live`. A live write needs an allowed tree (`write_trees`), a repeated `--confirm-tree` outside
sandbox trees, and passes a preflight (status, edit rights, pinned account); identical repeats within 24 hours are refused.
Edits and removals are snapshotted first and read back after. Success means the response body proved it, not HTTP 200.
Nothing is retried: a failure after a request left is an *unknown outcome* (exit code 3) to be checked with `journal verify`.
Failures carry `state`, `problems`, and machine-readable `next_actions`, so an agent can recover without parsing prose.
Every write is journaled (ids and the inverse operation, never names) so `journal undo` can reverse it. Details:
[docs/architecture.md](docs/architecture.md).

## Known limits

Person merge, tree-level operations, GEDCOM upload, stickies and comments are not covered. Linking an existing tree
person as a relative is not captured yet (adding a relative always creates a new person). Son, Daughter, Brother and
Sister use the same route as Father/Mother/Spouse but were not individually verified.

## Develop

```bash
uv sync && uv run pytest        # offline: a fake browser stands in for Chrome
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed (see LICENSE).
