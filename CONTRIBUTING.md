# Contributing

## Setup
```bash
uv sync
uv run pytest                      # offline: a fake browser stands in for Chrome
uv run python scripts/gen_docs.py  # regenerate docs/operations.md and docs/errors.md
uvx ruff check src --select F,E9,B,UP,SIM,PLE --ignore E501 --exclude src/ancestry_cli/vendor
```
After changing code, reinstall the command: `uv tool install --force --reinstall .`

## Adding a write operation
1. **Capture it.** In the signed-in Chrome, open DevTools, Network tab, Preserve log, clear, perform the action *once* on a throwaway
   person, and look at the request (method, path, body, response). Do not guess endpoints.
2. **Allow it in the bridge.** Add an entry to `src/ancestry_cli/vendor/api_endpoints.py` (group `writes`, `side_effect: True`,
   path template, body). The bridge refuses anything not listed. If it needs a new HTTP method, add it to `ChromeBrowserSession`.
3. **Register it** in `src/ancestry_cli/ops.py` with `@operation(...)`: summary, risk class, required/optional fields, undo, an
   example, and a **success test** that checks the response body (HTTP 200 is never enough; find the field that proves it worked).
4. **Make it undoable** if possible: add the inverse to `journal.inverse` (and a matching rule in `journal._reconcile`), and add the
   operation to `snapshots.SNAPSHOT_OPS` if it changes something existing.
5. **Test it** in `tests/` with the fake bridge (see `test_operations.py`), including a 200 response with a failure body.
6. **Run it once live** on a throwaway person, reverse it, and run `scripts/gen_docs.py`.

## Rules
- Output must not contain values from the tree except where a command exists to show them.
- Never store names or free text in the journal.
- Nothing retries. A failure after a request left is an unknown outcome.
- New failure modes get a row in `runtime.ERRORS` with `retryable`, `needs_human` and an actionable hint.
