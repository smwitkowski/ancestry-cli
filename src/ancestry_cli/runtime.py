"""Browser lane, service lock, error classification and quiet-output helpers (self-contained)."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import io
import json
import logging
import re
import time
import warnings
from collections import namedtuple
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import config

ORIGIN = "https://www.ancestry.com"
MIN_INTERVAL = 3.0      # seconds between Ancestry calls, across processes
BLOCK_MINUTES = 15      # after a bot challenge, refuse every call for this long
RESET_URL = ORIGIN + "/family-tree/trees"

# Every failing result whose classification is a key here gets `retryable`, `needs_human`, `hint` and `next_actions` added by
# `annotate`, so a caller (human or agent) never has to guess what to do next.
#   retryable   safe to re-run the identical command (after `retry_after` seconds when given)
#   needs_human something only the owner can do (sign in, solve a challenge, edit config)
#   actions     machine-readable recovery steps, in order; `{name}` is filled from the result, unfilled ones become <name>:
#     run        exact argv to execute            retry      re-run the same command
#     human      `say` what the owner must do     edit-args  `say` what to change in your arguments
#   `when` says when a step applies (after-human, after-verify, after-reset, after-retry_after).
Err = namedtuple("Err", "retryable needs_human hint actions")


def _run(*argv, when=None):
    return {"kind": "run", "argv": list(argv), **({"when": when} if when else {})}


def _human(say, when=None):
    return {"kind": "human", "say": say, **({"when": when} if when else {})}


def _edit(say):
    return {"kind": "edit-args", "say": say}


_RETRY = {"kind": "retry"}
_DOCTOR = _run("ancestry", "doctor")
_VERIFY = [_run("ancestry", "journal", "verify", "--id", "{journal_id}"),
           _run("ancestry", "journal", "resolve", "--id", "{journal_id}", "--as", "<ok|failed>", when="after-verify")]
_LIST_HINTS = [_run("ancestry", "hint", "list", "--tree", "{tree_id}", "--person", "{person_id}")]

ERRORS = {
    "chrome-not-found": Err(False, True, "Install Google Chrome (or Chromium), then run `ancestry init` again.", [_human("Install Google Chrome."), _run("ancestry", "init", when="after-human")]),
    "chrome-not-listening": Err(True, False, "Chrome did not open its debug port. Close any Chrome using the same profile and run `ancestry init` again.", [_run("ancestry", "init")]),
    "sign-in-timeout": Err(True, True, "Sign in to Ancestry in the Chrome window, then run `ancestry init` again.", [_human("Sign in to Ancestry in the Chrome window."), _run("ancestry", "init", when="after-human")]),
    # --- browser and session
    "browser-lane-not-running": Err(False, True, "Run `ancestry init` (or start Chrome with --remote-debugging-port={port} and a dedicated --user-data-dir), sign in to ancestry.com, and leave one tab open on www.ancestry.com.",
                                    [_human("Start Chrome with --remote-debugging-port={port}, sign in to ancestry.com and keep one tab open on www.ancestry.com."), _run("ancestry", "doctor", when="after-human")]),
    "browser-lane-ambiguous": Err(False, True, "Close the extra tabs (exactly one page may be open), or run `ancestry lane reset`.",
                                  [_human("Close all tabs but one."), _run("ancestry", "lane", "reset", when="after-human")]),
    "browser-lane-site-required": Err(False, False, "The tab is not on www.ancestry.com. Run `ancestry lane reset`.",
                                      [_run("ancestry", "lane", "reset"), _RETRY]),
    "browser-lane-target-invalid": Err(False, True, "Chrome returned an unexpected tab id. Close and reopen the tab.",
                                       [_human("Close and reopen the ancestry.com tab."), _DOCTOR]),
    "browser-lane-target-changed": Err(True, False, "The tab changed during the call. Retry.", [_RETRY]),
    "lane-page-unusable": Err(True, False, "The tab cannot run requests (often an error page). Run `ancestry lane reset`, then retry.",
                              [_run("ancestry", "lane", "reset"), {**_RETRY, "when": "after-reset"}]),
    "session-expired": Err(False, True, "Sign in to ancestry.com again in the Chrome window, then retry.",
                           [_human("Sign in to ancestry.com again in the Chrome window."), _run("ancestry", "doctor", when="after-human")]),
    "bot-challenge": Err(False, True, "Ancestry showed a bot challenge. Solve it in the Chrome window; calls are blocked for 15 minutes.",
                         [_human("Solve the challenge in the Chrome window."), _run("ancestry", "doctor", when="after-retry_after")]),
    "bot-challenge-block": Err(True, False, "Calls are paused after a bot challenge. Wait `retry_after` seconds, then retry.",
                               [{**_RETRY, "when": "after-retry_after"}]),
    "rate-limited": Err(True, False, "Ancestry is rate limiting. Wait `retry_after` seconds and retry.", [{**_RETRY, "when": "after-retry_after"}]),
    "preflight-failed": Err(True, False, "The page check returned an unexpected response. Retry; if it repeats run `ancestry doctor`.", [_RETRY, _DOCTOR]),
    "native-operation-failed": Err(True, False, "An unexpected error stopped the command before anything was sent. Retry once; if it repeats, run `ancestry doctor`.", [_RETRY, _DOCTOR]),
    "anchor-ids-unresolved": Err(True, False, "Could not read the anchor person's ids from the page. Retry; if it repeats, run `ancestry doctor`.", [_RETRY, _DOCTOR]),
    "eval-version-unresolved": Err(True, False, "The hints page did not expose the web app version. Retry; if it repeats the site layout may have changed.", [_RETRY, _DOCTOR]),
    "snapshot-failed": Err(True, False, "Could not save the before-snapshot, so nothing was sent. Retry.", [_RETRY]),
    # --- identity, rights, ids
    "person-not-in-tree": Err(False, False, "That person id does not exist in that tree (or was deleted). Check --tree and --person.",
                              [_run("ancestry", "find", "--tree", "{tree_id}", "--surname", "<surname>"), _edit("Use a person id from `find` or `person`.")]),
    "no-edit-rights": Err(False, True, "The signed-in account cannot edit this tree.",
                          [_human("Sign in as an account that can edit this tree, or choose another tree."), _run("ancestry", "trees")]),
    "wrong-account": Err(False, True, "The signed-in account differs from the pinned account_id. Sign in as the right account, or re-pin with `ancestry whoami --pin`.",
                         [_human("Sign in as the pinned account, or re-pin with `ancestry whoami --pin`."), _run("ancestry", "whoami")]),
    "actor-unresolved": Err(False, True, "Could not read the account id from the person page. Run `ancestry doctor`.", [_DOCTOR]),
    "account-unresolved": Err(False, True, "Could not tell the account from your owned trees. Run `ancestry doctor`.", [_DOCTOR]),
    "unknown-tree": Err(False, False, "Unknown tree id or alias.", [_run("ancestry", "trees")]),
    # --- write guards
    "tree-not-writable": Err(False, True, "This tree is not in write_trees for the active profile.",
                             [_human("If this tree should be writable, add its id to write_trees in ~/.ancestry-cli/config.json."), _run("ancestry", "trees")]),
    "confirm-tree-required": Err(False, False, "Repeat the tree id with --confirm-tree (typo guard for non-sandbox trees).", [_edit("Add --confirm-tree {tree_id}.")]),
    "duplicate-write": Err(False, False, "An identical write was recorded in the last 24 hours. Pass --force only if you really mean to repeat it.",
                           [_run("ancestry", "journal", "verify", "--id", "{journal_id}"), _edit("Add --force only if verify shows the earlier write did not happen.")]),
    "unknown-outcome": Err(False, False, "A request was sent but its result is unknown. Do not retry: run `ancestry journal verify --id <journal_id>`, then `ancestry journal resolve --id <journal_id> --as ok|failed`.", _VERIFY),
    "merge-job-failed": Err(False, False, "The hint upload started but did not finish, so the outcome is unknown. Do not retry: run `ancestry journal verify --id <journal_id>`.", _VERIFY),
    "confirmation-failed": Err(False, False, "The hint upload went through but was not confirmed, so the outcome is unknown. Do not retry: run `ancestry journal verify --id <journal_id>`.", _VERIFY),
    "privacy-incident": Err(False, False, "A dependency printed output that was discarded. The write may have happened: run `ancestry journal verify --id <journal_id>`.", _VERIFY),
    "send-failed": Err(False, False, "The server rejected the write; nothing changed. Check the fields (`ancestry ops <op>`) and the ids.",
                       [_run("ancestry", "ops", "{op}"), _edit("Fix the fields or ids; the server rejected this write.")]),
    # --- bad arguments
    "usage-error": Err(False, False, "Check `ancestry <command> --help`.", [_edit("Fix the arguments listed in `problems`.")]),
    "configuration-error": Err(False, False, "A tree id, option or combination is invalid for this command. Check `ancestry <command> --help`.", [_edit("Check the tree id and the options.")]),
    "invalid-write-request": Err(False, False, "A required field is missing or not allowed for this operation. See `problems` and `ancestry ops <op>`.",
                                 [_run("ancestry", "ops", "{op}"), _edit("Fix the fields listed in `problems`.")]),
    "fact-not-found": Err(False, False, "That assertion id is not on this person. Use `ancestry person` to list fact ids.", [_run("ancestry", "person", "--tree", "{tree_id}", "--person", "{person_id}")]),
    "invalid-media-file": Err(False, False, "Use an absolute path to a png, jpg, gif or webp file up to 25 MB.", [_edit("Use an absolute path to a png, jpg, gif or webp file up to 25 MB.")]),
    "unknown-tag": Err(False, False, "Unknown tag name. Use a listed tag name or its numeric id.", [_edit("Use a tag name from `did_you_mean`, or a numeric tag id.")]),
    "live-not-admitted": Err(False, False, "That operation cannot be sent live.", [_run("ancestry", "ops")]),
    "hint-id-required": Err(False, False, "Pass --hint-id (see `ancestry hint list`).", _LIST_HINTS),
    "hint-not-found-or-not-a-record": Err(False, False, "That hint is not among the person's new hints, or it has no record. List them with `ancestry hint list`.", _LIST_HINTS),
    "missing-arguments": Err(False, False, "Required options for this read are missing. See `ancestry read --help`.", [_edit("Provide the required options for this read.")]),
    "missing-path-param": Err(False, False, "This endpoint needs a path value (see `missing`); pass it with --path key=value.", [_edit("Add --path {missing}=<value>.")]),
    "side-effecting-read": Err(False, False, "That endpoint starts server-side work and is not offered as a read.", [_run("ancestry", "read", "list")]),
    "specify-id-or-all": Err(False, False, "Pass --id N to undo one entry, or --all (entries on non-sandbox trees need --id).", [_run("ancestry", "journal", "list")]),
    "tree-and-person-required": Err(False, False, "This endpoint needs both --tree and --person.", [_edit("Add --tree and --person.")]),
    "unknown-endpoint": Err(False, False, "No such read endpoint. List them with `ancestry read list`.", [_run("ancestry", "read", "list")]),
    "read-failed": Err(False, False, "The server returned an error for this read (see `status`). Nothing was changed.", [_edit("Check the ids and parameters; see `status`."), _DOCTOR]),
}


def suggest(value, valid, n=3):
    """Close matches from a fixed list (for "did you mean"). Only ever called with public vocabularies."""
    import difflib
    return difflib.get_close_matches(str(value), [str(v) for v in valid], n=n, cutoff=0.6)


class LaneError(Exception):
    """A failure with a stable classification; `detail` is extra safe context (field names, ids) for the result."""
    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


_ACTOR = re.compile(r"/user/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/tree/")


def actor_from_page(page_text, pinned=None):
    """The account's user id, read from the URLs a person page embeds. Raises LaneError(wrong-account | actor-unresolved)."""
    found = set(_ACTOR.findall(page_text.replace("\\/", "/")))
    if pinned:      # a shared tree's page may carry several user ids: require the pinned account to be one of them
        if pinned not in found:
            raise LaneError("wrong-account")
        return pinned
    if len(found) != 1:
        raise LaneError("actor-unresolved")
    return found.pop()


def failure(code, *, dispatched=False, state=None, **extra):
    """The standard failing result. `state` says what an agent may assume about the tree: "unchanged" when nothing
    was sent, "unknown" when a request left and the result is not known, "changed" when the change is confirmed."""
    return {"ok": False, "classification": code, "dispatch_attempted": dispatched,
            "state": state or ("unknown" if dispatched else "unchanged"), **extra}


def shape(value):
    """Key names and types only, so output never carries values (opaque numeric ids show as "id")."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return "id" if isinstance(value, str) and value.isdigit() else type(value).__name__


def _fill(text, ctx):
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else f"<{m.group(1)}>", text)


def annotate(result):
    """Add retryable / needs_human / hint / next_actions (and retry_after) to a failing result."""
    if not (isinstance(result, dict) and result.get("ok") is False and result.get("classification") in ERRORS):
        return result
    err = ERRORS[result["classification"]]
    ctx = {k: v for k, v in result.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}
    ctx["port"] = config.port()
    result.setdefault("retryable", err.retryable)
    result.setdefault("needs_human", err.needs_human)
    result.setdefault("hint", _fill(err.hint, ctx))
    actions = []
    for template in err.actions:
        action = dict(template)
        if "argv" in action:
            action["argv"] = [_fill(a, ctx) for a in action["argv"]]
        if "say" in action:
            action["say"] = _fill(action["say"], ctx)
        actions.append(action)
    result.setdefault("next_actions", actions)
    if "retry_after" not in result:
        if result["classification"] == "rate-limited":
            result["retry_after"] = 60
        elif result["classification"] in ("bot-challenge", "bot-challenge-block"):
            until = breaker_until()
            result["retry_after"] = max(1, int(until - time.time())) if until else BLOCK_MINUTES * 60
    return result


def classify_preflight(status, text):
    """Map a page-check response to an error code, or None when the page is usable."""
    body = (text or "").lstrip()
    is_json = body[:1] in ("{", "[")
    if status in (None, 0):
        return "session-expired"      # redirects are not followed, so a sign-in bounce arrives as status 0
    if status == 200:
        return None
    if status == 401:
        return "session-expired"
    if status == 403:
        return "session-expired" if is_json else "bot-challenge"
    if status == 429:
        return "rate-limited" if is_json else "bot-challenge"
    if status == 503 and not is_json:
        return "bot-challenge"
    if status == 404:
        return "person-not-in-tree"
    return "preflight-failed"


def classify_exception(exc):
    text = str(exc)   # lint: classify-only (matched against a known phrase, never returned)
    if "page-context fetch raised" in text:
        return "lane-page-unusable"
    return None


def load_bridge():
    from .vendor import ancestry_browser_bridge
    return ancestry_browser_bridge


# ---- bot-challenge circuit breaker -------------------------------------------------------------------------------
def _blocked_file():
    return config.lock_dir() / f"port-{config.port()}.blocked"


def trip_breaker(minutes=BLOCK_MINUTES):
    config.lock_dir().mkdir(parents=True, exist_ok=True)
    _blocked_file().write_text(str(time.time() + minutes * 60))


def breaker_until():
    """Epoch seconds the block ends, or None when calls are allowed."""
    try:
        until = float(_blocked_file().read_text())
    except (OSError, ValueError):
        return None
    return until if until > time.time() else None


@dataclass
class Lease:
    bridge: object
    target_id: str = ""

    @property
    def base(self):
        return f"http://127.0.0.1:{config.port()}"

    def pages(self):
        try:
            targets = self.bridge._cdp_json(self.base, "/json/list")
        except (Exception, SystemExit):
            raise LaneError("browser-lane-not-running") from None
        pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"] if isinstance(targets, list) else []
        if not pages:
            raise LaneError("browser-lane-not-running")
        if len(pages) != 1:
            raise LaneError("browser-lane-ambiguous")
        return pages[0]

    def check(self):
        """Exactly one page, on www.ancestry.com. Never opens, navigates or closes anything."""
        page = self.pages()
        tid = page.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid):
            raise LaneError("browser-lane-target-invalid")
        if self.target_id and tid != self.target_id:
            raise LaneError("browser-lane-target-changed")
        parsed = urlsplit(page.get("url", ""))
        if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
            raise LaneError("browser-lane-site-required")
        self.target_id = tid
        return self


def open_lane(bridge):
    """The configured lane, after the one-tab-on-ancestry.com check."""
    return Lease(bridge).check()


def lane_reset(bridge=None):
    """Navigate the single tab to a known-good page. Passive otherwise: needs exactly one page and the lock."""
    bridge = bridge or load_bridge()
    with lock():
        lease = Lease(bridge)
        page = lease.pages()                                  # raises not-running / ambiguous
        version = bridge._cdp_json(lease.base, "/json/version")
        ws = version.get("webSocketDebuggerUrl")
        if not ws:
            raise LaneError("browser-lane-not-running")

        async def navigate():
            import websockets
            async with websockets.connect(ws, max_size=16 * 1024 * 1024, open_timeout=15) as sock:
                cdp = bridge._CDP(sock)
                attached = await cdp.call("Target.attachToTarget", {"targetId": page["id"], "flatten": True})
                sid = attached["sessionId"]
                try:
                    await cdp.call("Page.navigate", {"url": RESET_URL}, session_id=sid)
                    await asyncio.sleep(3)
                finally:
                    with contextlib.suppress(Exception):
                        await cdp.call("Target.detachFromTarget", {"sessionId": sid})
        asyncio.run(navigate())
    return {"ok": True, "classification": "lane-reset", "navigated_to": RESET_URL}


@contextlib.contextmanager
def lock(service="ancestry"):
    """Cross-process exclusive lock plus pacing, per Chrome port: concurrent runs never overlap or burst."""
    until = breaker_until()
    if until:
        raise LaneError("bot-challenge-block")
    path = config.lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            last = float(handle.read().strip() or 0)
            delay = last + MIN_INTERVAL - time.time()
            if delay > 0:
                time.sleep(delay)
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.write(str(time.time()))
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def session():
    """lock + lane check + browser session in one place: yields (bridge, lease, inner)."""
    bridge = load_bridge()
    with lock():
        lease = open_lane(bridge)
        yield bridge, lease, bridge.ChromeBrowserSession(cdp_url=lease.base, target_id=lease.target_id)


def get_json(inner, path, params=None, timeout=60):
    """In-page GET -> parsed JSON. Raises LaneError(<classification>) for expired sessions, challenges, 404s..."""
    resp = inner.get(ORIGIN + path, params=params, timeout=timeout, allow_redirects=False, writes_ok=False)
    code = classify_preflight(getattr(resp, "status_code", None), getattr(resp, "text", ""))
    if code:
        raise LaneError(code)
    try:
        return json.loads(resp.text)
    except ValueError:
        raise LaneError("preflight-failed") from None


def guarded(fn):
    """Run fn() -> result dict; turn lane/classification errors into a failure result and trip the breaker."""
    try:
        with quiet():
            return fn()
    except LaneError as exc:
        if exc.code == "bot-challenge":
            trip_breaker()
        return {"ok": False, "classification": exc.code, "dispatch_attempted": False}
    except Exception as exc:
        return {"ok": False, "classification": classify_exception(exc) or "native-operation-failed", "dispatch_attempted": False}


@contextlib.contextmanager
def quiet():
    """Capture anything a dependency prints so it cannot leak into command output."""
    old = logging.root.manager.disable
    out, err = io.StringIO(), io.StringIO()
    logging.disable(2**63 - 1)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            warnings.simplefilter("error")
            yield out, err
    finally:
        logging.disable(old)
