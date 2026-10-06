# How it works

## The shape

```
 ancestry (CLI)  ->  ops / reads / hints / discovery / journal      what to do, and the guards
                          |
                       sender / runtime                             lock, lane, preflight, snapshot, send, journal
                          |
                  vendor/ancestry_browser_bridge  --CDP-->  your signed-in Chrome  -->  ancestry.com
```

There is **no Ancestry API key and no password handling**. You sign in once in a dedicated Chrome started with
`--remote-debugging-port`. Each request is a `fetch` run *inside that page* over the Chrome DevTools Protocol, so the
browser attaches its own session cookies and they never reach this tool. The endpoints and bodies were captured from the
web app's own traffic (HAR files), so the tool does what the web app does.

## The browser lane

`runtime.Lease` requires exactly one page target, on `https://www.ancestry.com`. It never opens, navigates or closes tabs
(only `ancestry lane reset` navigates, and only when you ask). A cross-process file lock (per Chrome port, in a fixed
per-user temp folder) serializes calls and spaces them 3 seconds apart. A bot challenge writes a 15-minute block file that
stops all calls.

## Anatomy of a live write (`sender.send`)

1. **Guards** in `ops.write`: `write_trees` allowlist, `--confirm-tree`, duplicate guard (request hash vs the journal).
2. **Open**: lock, lane check, then `GET` the person's facts page as a **preflight**: HTTP status first (expired session,
   bot challenge, missing person are classified, never scraped), then `HasTreeEditRights`, then the account id (checked against
   the pinned `account_id`). For edits and removals a **before-snapshot** is saved (mode 600); no snapshot, no write.
3. **Prepare**: fill what only the page knows: a fact edit starts from the fact's current values (an edit is a patch), a person
   removal reads the display name, a relative add reads the anchor's name/gender ids.
4. **Dispatch**: exactly one request (media upload is three: token, binary PUT, attach). From the moment it is sent, any
   exception is an **unknown outcome**, recorded as such and never called a failure.
5. **Finish**: the operation's own success test decides `ok` (HTTP 200 alone never does), a values-free **readback diff** is
   computed against the snapshot, the write is **journaled** (ids and inverse operation, no names), and the result is returned.

## State on disk (`~/.ancestry-cli/`)

| file | content |
|---|---|
| `config.json` | profiles, aliases, `write_trees`, `sandbox_trees`, pinned `account_id` |
| `journal.jsonl` | one row per write: op, ids, outcome (`ok` / `failed` / `unknown`), the inverse operation, request hash. Marker rows record `undo_of` and `resolved`. No names or text. |
| `snapshots/` | before-state of edits and removals (private data; mode 600). Referenced by journal rows. |

## Modules

| module | role |
|---|---|
| `cli.py` | argument parsing, dispatch, `doctor` |
| `ops.py` | the operation registry: spec, request builder and success test per operation; the guards; `write()` |
| `sender.py` | the live send, in steps (`_open`, `_prepare`, `_dispatch`, `_finish`) |
| `reads.py`, `hints.py`, `discovery.py` | reads, hint list/accept, `whoami` / `trees` / `find` / `person` |
| `journal.py`, `snapshots.py` | the undo log and before-state snapshots |
| `media.py`, `tags.py`, `merge_payload.py` | image upload planning, the 79 tag ids, the hint-accept payload builders |
| `runtime.py` | lane, lock, error table (`ERRORS`), preflight classification, circuit breaker |
| `config.py` | profiles, aliases, allowlist, paths |
| `vendor/` | the browser bridge and the endpoint inventory (below) |

## Vendored bridge

`vendor/ancestry_browser_bridge.py` and `vendor/api_endpoints.py` are copies of the older `tree-vault` scripts, with these
changes: the endpoint inventory is imported relatively; the default CDP port is 9224; `ChromeBrowserSession` gained
`patch()`, `delete()` and `put_binary()` (binary bodies travel as base64 and are rebuilt as bytes in the page); and the
per-upload `securityToken` is the single token-named query parameter allowed (only in `put_binary`). The bridge refuses any
request whose method and path are not in `api_endpoints.py`, so adding an operation means capturing the request and adding
its endpoint there too (see CONTRIBUTING).

## Safety properties

- A write needs an explicit `--live`, an allowed tree, a repeated tree id (outside sandboxes), a passing preflight and, for
  edits and removals, a saved snapshot.
- Nothing is retried. A failure after the request left is `unknown-outcome`, and a repeat is refused by the duplicate guard.
- Output never contains values from your tree except where a command exists to show them (`find`, `person`, `read --full`),
  and those redact people who may be living by default.
- The journal and snapshots are local and private; they contain ids, not names or text.
