# Making errors more helpful to agents

From an Opus review of the error output (2026-10-06). Everything below is implemented unless listed under "Not doing".

## Principles
- `classification` is the stable code. Add fields; never rename or remove existing ones.
- Every failure says what may be assumed about the tree: `state` is `unchanged` (nothing was sent), `unknown` (a request left and
  the result is not known) or `changed` (confirmed). **done** (`runtime.failure`, `sender`, `hints`)
- Messages: one sentence for what happened, one for what to do, at most 200 characters. Name fields and steps, never values. Allowed:
  field names, fixed vocabularies (tags, relations, op names, endpoint names), config aliases, numeric ids, HTTP status, the
  server's integer `ErrorCode`. Never: input values, response bodies, `str(exc)`, raw argparse messages.
- Nothing retries. `unknown` always means "not retryable; verify first".

## Done
1. `journal_id` on every write result and on `unknown-outcome`, so the agent knows which entry to `journal verify`. Hint-accept rows
   now store the request hash, so an unfinished accept cannot be blindly retried (the duplicate guard sees it).
2. Every code the tool emits is in `runtime.ERRORS` (a source-scanning test enforces it); `merge-job-failed` and
   `confirmation-failed` are "do not retry, verify" codes.
3. A 5xx, a timeout, or a 2xx whose body does not confirm success is an **unknown outcome**; only an HTTP 4xx or an explicit
   negative body (`ErrorCode` != 0, `success: false`, `status: false`) is a plain `send-failed`.
4. `missing-path-param` is a fixed code with `missing: <name>` instead of embedding the name in the code. `_apply_profile_flag` duplicate removed.

## Also done (second pass)
5. Argparse errors keep their structure: unknown tree alias, invalid choice, missing/invalid/unrecognized arguments become `problems`
   (option names and allowed choices, never the user's value), with `did_you_mean`.
6. All field problems are found up front from each operation's spec and reported together (`missing`, `invalid`, `not-allowed`,
   `unknown-value` with `valid_values`); dry-runs return the specific code instead of `configuration-error`. Every write result names
   its `op`, `tree_id` and `person_id`.
7. `did_you_mean` (difflib over fixed vocabularies): operations, relations, tree aliases, tags, endpoints, field names.
8. `next_actions` on every code in the catalog (a test checks that every `run` argv parses as a real command, and that every retryable or
   human-only code has an action); `retry_after` for `rate-limited` and the bot-challenge block.
9. Multi-request operations report `progress: {done, failed_at}`; a half-done media upload leaves the tree unchanged and warns
   `orphan-media`. Hint accepts report progress too. `warnings` for non-fatal issues (`readback-failed`, `journal-error`);
   `details.server_error_code` carries the server's integer code, never its text.
10. Exit code `3` for an unknown state. A lint test forbids exception text in output. A scripted "agent" test suite exercises recovery from
    an expired session, a wrong person id, a missing field, a confirmation guard, an unknown outcome (including an impatient blind retry
    that the duplicate guard stops), and a bot challenge.

## Not doing
Golden-file tests for every code and a privacy canary test (decided against; the lint and the catalog test cover the main risks).

## Example payloads (target shape)
```json
{"ok": false, "classification": "invalid-write-request", "dispatch_attempted": false, "state": "unchanged",
 "message": "2 problems with relative-add fields.",
 "problems": [{"field": "status", "issue": "missing", "valid_values": ["Living", "Deceased"]},
              {"field": "relation", "issue": "unknown-value", "valid_values": ["Father", "Mother", "Spouse", "Son", "Daughter", "Brother", "Sister"]}],
 "next_actions": [{"kind": "run", "argv": ["ancestry", "ops", "relative-add"]}]}

{"ok": false, "classification": "unknown-outcome", "dispatch_attempted": true, "state": "unknown", "journal_id": 41,
 "retryable": false, "needs_human": false,
 "next_actions": [{"kind": "run", "argv": ["ancestry", "journal", "verify", "--id", "41"]},
                  {"kind": "run", "when": "after-verify", "argv": ["ancestry", "journal", "resolve", "--id", "41", "--as", "<ok|failed>"]}]}
```
