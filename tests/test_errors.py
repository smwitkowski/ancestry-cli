"""Error quality: structured problems, did-you-mean, next_actions that are real commands, state, exit codes, warnings, and a
scripted "agent" that recovers from faults by following the guidance."""
import json
import re
from pathlib import Path

import pytest

import ancestry_cli.runtime as rt
from ancestry_cli import cli, config, journal as tj, ops, sender

TREE, PERSON = 111111111, 555555555555
ACTOR = "00000000-0000-0000-0000-000000000099"
CANARY = "ZZCANARY"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"sandbox_trees": [TREE], "trees": {"sandbox": TREE, "main": 1}}))
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    monkeypatch.setenv("ANCESTRY_CLI_SNAPSHOTS", str(tmp_path / "snaps"))
    monkeypatch.setattr(config, "lock_dir", lambda: tmp_path)
    monkeypatch.setattr(rt, "MIN_INTERVAL", 0)
    monkeypatch.delenv("ANCESTRY_CLI_PROFILE", raising=False)
    return tmp_path


def run(capsys, *argv):
    """Run the CLI like a shell would: returns (exit code, parsed JSON)."""
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------------------------------- argparse -> problems
def test_argparse_errors_become_structured_problems_without_echoing_values(env, capsys):
    code, out = run(capsys, "write", "fact-add", "--tree", CANARY, "--person", "1")
    assert code == 2 and out["classification"] == "unknown-tree" and CANARY not in json.dumps(out)
    p = out["problems"][0]
    assert p["field"] == "--tree" and p["issue"] == "unknown-value" and "sandbox" in p["valid_values"]
    code, out = run(capsys, "write", "fact-ad", "--tree", "sandbox", "--person", "1")           # a typo'd operation
    p = out["problems"][0]
    assert p["issue"] == "unknown-value" and "fact-add" in p["valid_values"] and p["did_you_mean"][0] == "fact-add"
    code, out = run(capsys, "write", "fact-add", "--tree", "sandbox")                            # missing --person
    assert out["problems"] == [{"field": "--person", "issue": "missing"}] and out["classification"] == "usage-error"
    code, out = run(capsys, "write", "fact-add", "--tree", "sandbox", "--person", CANARY)
    assert out["problems"][0]["issue"] == "invalid" and CANARY not in json.dumps(out)
    code, out = run(capsys, "write", "fact-add", "--tree", "sandbox", "--person", "1", "--bogus", CANARY)
    assert out["problems"][0] == {"field": "--bogus", "issue": "not-allowed"} and CANARY not in json.dumps(out)
    assert run(capsys, "nonsense")[1]["problems"][0]["issue"] == "unknown-value"


# ---------------------------------------------------------------------------------------------------- validation
def test_all_field_problems_are_reported_together_with_valid_values(env):
    out = ops.write(op="relative-add", tree_id=TREE, person_id=PERSON, relation="Fathr", bogus=CANARY)
    fields = {p["field"]: p for p in out["problems"]}
    assert out["classification"] == "invalid-write-request" and out["state"] == "unchanged" and out["op"] == "relative-add"
    assert fields["status"]["issue"] == "missing" and fields["status"]["valid_values"] == ["Living", "Deceased"]
    assert fields["relation"]["issue"] == "unknown-value" and fields["relation"]["did_you_mean"] == ["Father"]
    assert fields["bogus"]["issue"] == "not-allowed" and CANARY not in json.dumps(out)
    assert "3 problems" in out["message"]
    assert [p["field"] for p in ops.write(op="fact-edit", tree_id=TREE, person_id=PERSON)["problems"]] == ["--assertion"]
    bad_id = ops.write(op="fact-remove", tree_id=TREE, person_id=PERSON, assertion_id="abc")
    assert bad_id["problems"][0] == {"field": "assertion_id", "issue": "invalid", "expected": "a positive integer id"}
    assert ops.write(op="note-set", tree_id=TREE, person_id=PERSON, text="")["ok"] is True           # clearing a note is allowed
    assert ops.write(op="nope", tree_id=TREE, person_id=PERSON)["problems"][0]["valid_values"] == list(ops.SPECS)


def test_specific_codes_survive_the_dry_run_with_their_problems(env, tmp_path):
    out = ops.write(op="tag-add", tree_id=TREE, person_id=PERSON, tags="To-Do, Brik Wall")
    assert out["classification"] == "unknown-tag" and out["problems"][0]["did_you_mean"] == ["BrickWall"]
    out = ops.write(op="media-upload", tree_id=TREE, person_id=PERSON, file="relative.png", title="t")
    assert out["classification"] == "invalid-media-file" and out["problems"][0]["field"] == "file"
    out = ops.write(op="fact-add", tree_id=0, person_id=PERSON, eventType="x")
    assert out["classification"] == "configuration-error" and out["problems"][0]["field"] == "tree_id"


# ---------------------------------------------------------------------------------------------------- next_actions
def _argv_parses(argv):
    """An action's argv with <placeholders> replaced by plausible values must parse as a real ancestry command."""
    args = [re.sub(r"<([^>|]+)\|([^>]+)>", r"\1", a) for a in argv[1:]]          # <ok|failed> -> ok
    args = [re.sub(r"<[^>]+>", "1", a) for a in args]
    cli.build_parser().parse_args(args)


def test_every_next_action_is_a_real_command_and_actionable_codes_have_one():
    for code, err in rt.ERRORS.items():
        if err.needs_human or err.retryable:
            assert err.actions, f"{code} needs a next action"
        for action in err.actions:
            assert action["kind"] in ("run", "retry", "human", "edit-args"), code
            if action["kind"] == "run":
                assert action["argv"][0] in ("ancestry", "familysearch"), code
                if action["argv"][0] == "familysearch":
                    from ancestry_cli import familysearch
                    familysearch.build_parser().parse_args(action["argv"][1:])
                    continue
                _argv_parses(rt.annotate({"ok": False, "classification": code, "journal_id": 1, "tree_id": 1, "person_id": 2,
                                          "op": "fact-add", "missing": "k"})["next_actions"][err.actions.index(action)]["argv"])
            else:
                assert action.get("say") or action["kind"] == "retry", code


def test_next_actions_are_filled_in_from_the_result():
    out = rt.annotate(rt.failure("unknown-outcome", dispatched=True, journal_id=41))
    assert out["next_actions"][0] == {"kind": "run", "argv": ["ancestry", "journal", "verify", "--id", "41"]}
    assert out["next_actions"][1]["argv"][-1] == "<ok|failed>" and out["next_actions"][1]["when"] == "after-verify"
    out = rt.annotate(rt.failure("confirm-tree-required", tree_id=7))
    assert out["next_actions"] == [{"kind": "edit-args", "say": "Add --confirm-tree 7."}]
    out = rt.annotate(rt.failure("browser-lane-not-running"))
    assert "--remote-debugging-port=9224" in out["next_actions"][0]["say"]
    assert rt.annotate(rt.failure("rate-limited"))["retry_after"] == 60
    assert rt.annotate(rt.failure("bot-challenge-block"))["retry_after"] > 0
    assert "retry_after" not in rt.annotate(rt.failure("person-not-in-tree"))


# ---------------------------------------------------------------------------------------------------- exit codes
def test_exit_codes(env, capsys, monkeypatch):
    assert run(capsys, "ops")[0] == 0
    assert run(capsys, "write", "fact-add", "--tree", "sandbox")[0] == 2                      # bad usage
    monkeypatch.setattr(cli, "_run", lambda c, a: rt.failure("person-not-in-tree"))
    assert run(capsys, "trees")[0] == 1                                                         # failed, nothing changed
    monkeypatch.setattr(cli, "_run", lambda c, a: rt.failure("unknown-outcome", dispatched=True, journal_id=3))
    assert run(capsys, "trees")[0] == 3                                                         # state unknown: stop and verify


# ---------------------------------------------------------------------------------------------------- lint
def test_no_exception_text_reaches_output():
    """str(exc)/repr(exc)/{exc} would leak user text or server messages into results; codes carry the meaning instead."""
    bad = []
    for path in (Path(__file__).resolve().parents[1] / "src" / "ancestry_cli").glob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "lint: classify-only" not in line and re.search(r"\bstr\(exc\)|\brepr\(exc\)|\{exc\}|\{exc\.|\bstr\(e\)|\{e\}", line):
                bad.append(f"{path.name}:{n}")
    assert not bad, bad


# ---------------------------------------------------------------------------------------------------- fake browser for the rest
class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status


class Browser:
    """A scriptable stand-in for the signed-in Chrome: counts writes and can fail on demand."""
    def __init__(self, page_status=200, post_body=None, post_status=200, put_status=200, boom_after_send=False, stoken_ok=True):
        self.posts, self.puts = 0, 0
        self.page_status, self.post_body, self.post_status, self.put_status = page_status, post_body, post_status, put_status
        self.boom, self.stoken_ok = boom_after_send, stoken_ok
        outer = self
        pr = {"PersonFullName": "ZZTEST A", "IsPersonLiving": False, "PersonSources": [], "PersonWebLinks": [], "PersonFamily": {},
              "HasTreeEditRights": True, "PersonFacts": [{"AssertionId": "5", "Type": 74, "TypeString": "Residence", "Date": "1901"}]}
        self.page = f"x /user/{ACTOR}/tree/{TREE}/person/{PERSON} y " + '<script id="person-data">' + json.dumps({"person": {"PersonResearch": pr}}) + "</script>"

        class ChromeBrowserSession:
            def __init__(self, cdp_url=None, target_id=None):
                pass

            def get(self, url, **kw):
                if "/stoken" in url:
                    return _Resp(json.dumps({"stoken": "t"}))
                if "getpersonnotes" in url:
                    return _Resp(json.dumps({"note": ""}))
                return _Resp(outer.page if outer.page_status == 200 else "", outer.page_status)

            def post(self, url, **kw):
                outer.posts += 1
                if outer.boom:
                    raise RuntimeError("boom")
                return _Resp(json.dumps(outer.post_body) if outer.post_body is not None else "{}", outer.post_status)

            def put_binary(self, url, **kw):
                outer.puts += 1
                return _Resp("", outer.put_status)
        self.ChromeBrowserSession = ChromeBrowserSession

    def _cdp_json(self, base, path):
        return [{"type": "page", "id": "T1", "url": f"https://www.ancestry.com/family-tree/person/tree/{TREE}/person/{PERSON}/facts"}]


@pytest.fixture
def browser(env, monkeypatch):
    holder = {}
    monkeypatch.setattr(rt, "load_bridge", lambda: holder["b"])      # the real lock runs (in a temp folder, no pacing)

    def use(**kw):
        holder["b"] = Browser(**kw)
        return holder["b"]
    use()
    return use


def write(capsys, op, *extra):
    return run(capsys, "write", op, "--tree", "sandbox", "--person", str(PERSON), *extra, "--live")


# ---------------------------------------------------------------------------------------------------- state, warnings, progress
def test_state_at_every_failure_point_matches_the_journal(browser, capsys):
    b = browser(page_status=0)                                                                  # preflight refuses
    code, out = write(capsys, "note-set", "--set", "text=a")
    assert (out["classification"], out["state"], b.posts, tj._rows()) == ("session-expired", "unchanged", 0, [])
    b = browser(post_body={"id": "1", "txt": "x"})                                              # confirmed
    code, out = write(capsys, "note-set", "--set", "text=b")
    assert (code, out["state"], out["journal_id"], tj._rows()[-1]["outcome"]) == (0, "changed", 0, "ok")
    browser(post_body={"error": "no"}, post_status=422)                                         # the server clearly said no
    code, out = write(capsys, "note-set", "--set", "text=c")
    assert (code, out["classification"], out["state"], tj._rows()[-1]["outcome"]) == (1, "send-failed", "unchanged", "failed")
    assert out["details"]["status"] == 422
    browser(post_body={"x": 1}, post_status=500)                                                # a 5xx: it may have been applied
    code, out = write(capsys, "note-set", "--set", "text=d")
    assert (code, out["classification"], out["state"], tj._rows()[-1]["outcome"]) == (3, "unknown-outcome", "unknown", "unknown")
    browser(boom_after_send=True)                                                               # an exception after the request left
    code, out = write(capsys, "note-set", "--set", "text=e")
    assert (code, out["state"], out["journal_id"]) == (3, "unknown", tj._rows()[-1]["id"])
    assert out["next_actions"][0]["argv"] == ["ancestry", "journal", "verify", "--id", str(out["journal_id"])]


def test_server_error_code_is_reported_as_an_integer(browser, capsys):
    browser(post_body={"ErrorCode": 3})
    code, out = write(capsys, "fact-attach-source", "--assertion", "5", "--set", "citation_id=7")
    assert out["classification"] == "send-failed" and out["details"]["server_error_code"] == 3


def test_half_finished_media_upload_reports_progress(browser, capsys, tmp_path):
    import struct, zlib
    raw = b"".join(b"\x00" + b"\xc8\x28\x28" * 4 for _ in range(4))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = tmp_path / "t.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))
    args = ("--set", f"file={png}", "--set", "title=t")
    browser(put_status=400)                                                                      # the stream upload is refused
    code, out = write(capsys, "media-upload", *args)
    assert out["classification"] == "send-failed" and out["progress"] == {"done": ["token"], "failed_at": "stream"}
    browser(post_status=400, post_body={"e": 1})                                                 # stored, but the attach is refused
    code, out = write(capsys, "media-upload", *args, "--force")
    assert out["state"] == "unchanged" and out["progress"] == {"done": ["token", "stream"], "failed_at": "attach"}
    assert [w["code"] for w in out["warnings"]] == ["orphan-media"]


def test_warnings_appear_on_successful_results(browser, capsys, monkeypatch):
    browser(post_body={"id": "1", "txt": "x"})
    monkeypatch.setattr(sender, "_readback", lambda w, f: {"error": "readback-failed"})
    monkeypatch.setattr(tj, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    code, out = write(capsys, "note-set", "--set", "text=w")
    assert code == 0 and out["state"] == "changed" and {w["code"] for w in out["warnings"]} == {"readback-failed", "journal-error"}


# ---------------------------------------------------------------------------------------------------- the scripted "agent"
def follow(capsys, result, kind="run", index=0):
    """Do what an agent would: execute the Nth next_action of the given kind."""
    action = [a for a in result["next_actions"] if a["kind"] == kind][index]
    return run(capsys, *action["argv"][1:])


def test_recovery_from_an_unknown_outcome_never_duplicates_a_write(browser, capsys, monkeypatch):
    b = browser(boom_after_send=True)
    _, first = write(capsys, "note-set", "--set", "text=once")
    assert first["classification"] == "unknown-outcome" and b.posts == 1
    _, again = write(capsys, "note-set", "--set", "text=once")                 # the impatient agent retries blindly...
    assert again["classification"] == "duplicate-write" and b.posts == 1       # ...and the guard stops it: still one request
    monkeypatch.setattr("ancestry_cli.discovery.person", lambda **k: {"ok": True, "facts": []})
    code, verified = follow(capsys, again)                                       # the duplicate result also points at verify
    assert code == 0 and verified["classification"] == "verify"
    code, resolved = run(capsys, "journal", "resolve", "--id", str(first["journal_id"]), "--as", "failed")
    assert code == 0
    b.boom = False
    b.post_body = {"id": "1", "txt": "x"}
    _, retried = write(capsys, "note-set", "--set", "text=once")                # after verify + resolve, the retry is allowed
    assert retried["ok"] is True and b.posts == 2


def test_recovery_guidance_for_common_faults(browser, capsys, env):
    browser(page_status=0)
    _, out = write(capsys, "note-set", "--set", "text=x")                      # session expired
    assert out["needs_human"] and [a["kind"] for a in out["next_actions"]] == ["human", "run"]
    assert out["next_actions"][1]["when"] == "after-human" and out["state"] == "unchanged"
    browser(page_status=404)
    _, out = write(capsys, "note-set", "--set", "text=x")                      # wrong person id
    assert out["classification"] == "person-not-in-tree" and out["next_actions"][0]["argv"][:2] == ["ancestry", "find"]
    browser()
    _, out = run(capsys, "write", "fact-add", "--tree", "main", "--person", "1", "--set", "eventType=x", "--live")
    assert out["classification"] == "confirm-tree-required"                    # not a sandbox tree
    assert out["next_actions"][0]["say"] == "Add --confirm-tree 1."
    _, out = write(capsys, "relative-add", "--set", "relation=Mother")          # fields missing: follow `ops`
    assert out["classification"] == "invalid-write-request" and out["next_actions"][0]["argv"] == ["ancestry", "ops", "relative-add"]
    code, described = follow(capsys, out)
    assert code == 0 and described["op"] == "relative-add" and described["required"] == ["relation", "status"]


def test_a_bot_challenge_pauses_every_later_call_and_says_for_how_long(browser, capsys):
    browser(page_status=403)
    _, out = write(capsys, "note-set", "--set", "text=x")
    assert out["classification"] == "bot-challenge" and out["needs_human"] and out["retry_after"] > 0
    _, later = write(capsys, "note-set", "--set", "text=x")
    assert later["classification"] == "bot-challenge-block" and later["retryable"] and later["retry_after"] > 0
    assert later["next_actions"] == [{"kind": "retry", "when": "after-retry_after"}]


def test_a_code_that_can_follow_a_sent_request_is_never_retryable():
    """The docs promise: retryable is never true when the state is unknown. These codes are the ones emitted after a request left."""
    for code in ("unknown-outcome", "merge-job-failed", "confirmation-failed", "privacy-incident", "send-failed"):
        assert rt.ERRORS[code].retryable is False, code
        assert any(a["kind"] == "run" for a in rt.ERRORS[code].actions), code
    for code in ("unknown-outcome", "merge-job-failed", "confirmation-failed", "privacy-incident"):
        assert rt.annotate(rt.failure(code, dispatched=True, journal_id=2))["state"] == "unknown"
