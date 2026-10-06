"""Auth/session classification, profiles and aliases, discovery commands, duplicate guard, circuit breaker."""
import contextlib
import json
import time

import pytest

import ancestry_cli.runtime as rt
from ancestry_cli import cli, config, discovery, journal as tj, ops as tw, sender as live

TREE, PERSON = 111111111, 555555555555
ACTOR = "00000000-0000-0000-0000-000000000099"


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("status,text,code", [
    (200, "{}", None), (0, "", "session-expired"), (None, "", "session-expired"), (401, "{}", "session-expired"),
    (403, '{"message":"x"}', "session-expired"), (403, "<html>challenge</html>", "bot-challenge"),
    (429, "<html>", "bot-challenge"), (429, '{"x":1}', "rate-limited"), (503, "<html>", "bot-challenge"),
    (404, "", "person-not-in-tree"), (500, "", "preflight-failed")])
def test_classify_preflight(status, text, code):
    assert rt.classify_preflight(status, text) == code


def test_every_error_code_carries_retry_and_human_flags():
    for code in rt.ERRORS:
        out = rt.annotate({"ok": False, "classification": code})
        assert isinstance(out["retryable"], bool) and isinstance(out["needs_human"], bool) and out["hint"]
    assert rt.annotate({"ok": True, "classification": "read"}) == {"ok": True, "classification": "read"}
    assert rt.annotate({"ok": False, "classification": "something-new"}) == {"ok": False, "classification": "something-new"}
    assert rt.classify_exception(RuntimeError("page-context fetch raised an exception")) == "lane-page-unusable"
    assert rt.classify_exception(RuntimeError("other")) is None


# ---------------------------------------------------------------- profiles, aliases, pinning
def _write_config(tmp_path, monkeypatch, data):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(data))


def test_simple_config_is_the_default_profile(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"port": 9111, "sandbox_trees": [1], "write_trees": [1, 2], "trees": {"main": 2},
                                         "account_id": "A"})
    monkeypatch.delenv("ANCESTRY_CLI_PORT", raising=False)
    assert config.port() == 9111 and config.account_id() == "A"
    assert config.resolve_tree("main") == 2 and config.resolve_tree("77") == 77 and config.resolve_tree(5) == 5
    with pytest.raises(ValueError):
        config.resolve_tree("nope")
    assert config.writable(2) and not config.writable(3)


def test_profiles_switch_port_trees_and_account(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"default_profile": "main", "profiles": {
        "main": {"port": 9224, "trees": {"t": 10}, "write_trees": [10]},
        "other": {"port": 9333, "trees": {"t": 20}, "account_id": "B"}}})
    monkeypatch.delenv("ANCESTRY_CLI_PORT", raising=False)
    assert (config.port(), config.resolve_tree("t"), config.write_trees()) == (9224, 10, frozenset({10}))
    monkeypatch.setenv("ANCESTRY_CLI_PROFILE", "other")
    assert (config.port(), config.resolve_tree("t"), config.write_trees(), config.account_id()) == (9333, 20, None, "B")
    assert config.writable(999)                      # no allowlist configured -> unrestricted (confirm-tree still applies)
    config.pin_account("C")
    assert json.loads((tmp_path / "config.json").read_text())["profiles"]["other"]["account_id"] == "C"


def test_write_trees_allowlist_blocks_live_writes_and_duplicate_guard(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"sandbox_trees": [TREE], "write_trees": [TREE]})
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    out = tw.write(op="fact-remove", tree_id=999, person_id=PERSON, assertion_id=5, dry_run=False, confirm_tree=999)
    assert out["classification"] == "tree-not-writable"
    sent = []
    monkeypatch.setattr(live, "send", lambda **k: sent.append(k) or {"ok": True})
    kw = dict(op="fact-remove", tree_id=TREE, person_id=PERSON, assertion_id=5, dry_run=False)
    assert tw.write(**kw)["ok"] is True and len(sent) == 1
    h = sent[0]["req_hash"]
    tj.record("fact-remove", TREE, PERSON, {"assertion_id": "5"}, {}, req_hash=h)         # that write is now on record
    dup = tw.write(**kw)
    assert dup["classification"] == "duplicate-write" and dup["journal_id"] == 0 and len(sent) == 1
    assert tw.write(**kw, force=True)["ok"] is True and len(sent) == 2
    tj.record("fact-remove", TREE, PERSON, {"assertion_id": "9"}, {}, outcome="failed", req_hash="other")
    assert tj.find_duplicate("other") is None                                              # failed writes do not block a retry


# ---------------------------------------------------------------- circuit breaker + lock
def test_bot_challenge_trips_a_block_that_stops_all_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lock_dir", lambda: tmp_path)
    assert rt.breaker_until() is None
    rt.trip_breaker(minutes=15)
    assert rt.breaker_until() > time.time() + 800
    with pytest.raises(rt.LaneError) as exc, rt.lock():
        pass
    assert exc.value.code == "bot-challenge-block"
    (tmp_path / f"port-{config.port()}.blocked").write_text(str(time.time() - 1))
    assert rt.breaker_until() is None


def test_lock_file_is_per_port_in_a_fixed_user_dir(monkeypatch):
    monkeypatch.setenv("ANCESTRY_CLI_PORT", "9999")
    assert str(config.lock_dir()).endswith(f"ancestry-cli-locks-{__import__('os').getuid()}")


# ---------------------------------------------------------------- live write preflight
class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status


class _Bridge:
    def __init__(self, page_status=200, page_text=None, rights=True):
        self.calls = []
        pr = {"PersonFullName": "ZZTEST A", "IsPersonLiving": False, "PersonSources": [], "PersonWebLinks": [], "PersonFamily": {},
              "HasTreeEditRights": rights, "PersonFacts": []}
        text = page_text if page_text is not None else (f"x /user/{ACTOR}/tree/{TREE}/person/{PERSON} y "
                                                        '<script id="person-data">' + json.dumps({"person": {"PersonResearch": pr}}) + "</script>")
        outer = self

        class ChromeBrowserSession:
            def __init__(self, cdp_url=None, target_id=None):
                pass

            def get(self, url, **kw):
                outer.calls.append(("GET", url))
                return _Resp(text, page_status)

            def post(self, url, **kw):
                outer.calls.append(("POST", url))
                return _Resp(json.dumps({"success": True, "returnUrl": "x"}))
        self.ChromeBrowserSession = ChromeBrowserSession

    def _cdp_json(self, base, path):
        return [{"type": "page", "id": "T1", "url": f"https://www.ancestry.com/family-tree/person/tree/{TREE}/person/{PERSON}/facts"}]


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "lock", lambda s="ancestry": contextlib.nullcontext())
    monkeypatch.setattr(config, "lock_dir", lambda: tmp_path)
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    monkeypatch.setenv("ANCESTRY_CLI_SNAPSHOTS", str(tmp_path / "snaps"))
    monkeypatch.setattr(config, "HOME", tmp_path)


@pytest.mark.parametrize("status,text,code", [(0, "", "session-expired"), (404, "", "person-not-in-tree"),
                                              (403, "<html>", "bot-challenge"), (429, '{"a":1}', "rate-limited")])
def test_a_failed_preflight_is_classified_and_nothing_is_sent(lane, status, text, code):
    b = _Bridge(page_status=status, page_text=text)
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=b, name="x")
    assert out["classification"] == code and out["dispatch_attempted"] is False
    assert not any(c[0] == "POST" for c in b.calls)
    assert rt.annotate(out)["needs_human"] == rt.ERRORS[code][1]
    assert (rt.breaker_until() is not None) == (code == "bot-challenge")


def test_no_edit_rights_refuses_the_write(lane):
    b = _Bridge(rights=False)
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=b, name="x")
    assert out["classification"] == "no-edit-rights" and not any(c[0] == "POST" for c in b.calls)


def test_pinned_account_must_be_on_the_page(lane, monkeypatch):
    monkeypatch.setattr(config, "account_id", lambda: "00000000-0000-0000-0000-0000000000ff")
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=_Bridge(), name="x")
    assert out["classification"] == "wrong-account"
    monkeypatch.setattr(config, "account_id", lambda: ACTOR)
    assert live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=_Bridge(), name="x")["ok"]


def test_person_remove_reads_the_name_from_the_page_and_the_journal_stores_no_names(lane):
    b = _Bridge()
    sent = {}
    orig = b.ChromeBrowserSession.post
    b.ChromeBrowserSession.post = lambda self, url, **kw: (sent.update(body=json.loads(kw["data"])), orig(self, url, **kw))[1]
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=b)     # no name given
    assert out["ok"] and sent["body"] == {"name": "ZZTEST A"}
    assert "ZZTEST" not in (config.journal_path().read_text())


def test_an_exception_after_the_send_is_an_unknown_outcome(lane, monkeypatch):
    monkeypatch.setattr(live, "succeeded", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=_Bridge(), name="x")
    assert out["classification"] == "unknown-outcome" and out["dispatch_attempted"] is True
    assert rt.annotate(out)["retryable"] is False


# ---------------------------------------------------------------- journal verify / resolve
def test_unknown_outcomes_are_listed_until_resolved(lane, monkeypatch):
    tj.record("relative-add", TREE, PERSON, {}, {}, outcome="unknown")
    assert [r["id"] for r in tj.manual()] == [0]
    monkeypatch.setattr("ancestry_cli.discovery.person", lambda **k: {"ok": True, "facts": []})
    v = tj.command(action="verify", entry_id=0)
    assert v["ok"] and v["entry"]["outcome"] == "unknown" and v["person"] == {"ok": True, "facts": []}
    assert tj.command(action="resolve", entry_id=0)["classification"] == "usage-error"      # must say ok|failed
    assert tj.command(action="resolve", entry_id=0, resolve_as="failed")["ok"]
    assert tj.manual() == [] and tj.outstanding() == []
    assert tj.command(action="verify", entry_id=99)["classification"] == "usage-error"


# ---------------------------------------------------------------- discovery
def _fake_session(monkeypatch, responses):
    @contextlib.contextmanager
    def session():
        yield None, None, "inner"
    monkeypatch.setattr(discovery, "session", session)
    monkeypatch.setattr(discovery, "get_json", lambda inner, path, params=None, **k: responses[(path, (params or {}).get("rights"))]
                        if "treesui" in path else responses[path])


def test_whoami_pins_the_owner_account(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {})
    _fake_session(monkeypatch, {("/api/treesui-list/trees", "own"): {"trees": [{"id": "1", "ownerUserId": "GUID1"}, {"id": "2", "ownerUserId": "GUID1"}]},
                                ("/api/treesui-list/trees", "shared"): {"trees": [{"id": "9", "ownerUserId": "OTHER"}]}})
    r = discovery.whoami()
    assert r["account_id"] == "GUID1" and r["pinned"] is None and r["matches_pin"] is None
    r = discovery.whoami(pin=True)
    assert r["pinned"] == "GUID1" and r["matches_pin"] is True and config.account_id() == "GUID1"


def test_trees_lists_owned_and_shared_with_alias_and_writable(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"trees": {"main": 1}, "write_trees": [1], "sandbox_trees": [2]})
    _fake_session(monkeypatch, {("/api/treesui-list/trees", "own"): {"trees": [{"id": "1", "name": "Main", "dateModified": "d"}, {"id": "2", "name": "Test"}]},
                                ("/api/treesui-list/trees", "shared"): {"trees": [{"id": "9", "name": "Theirs"}]}})
    rows = {r["tree_id"]: r for r in discovery.trees()["trees"]}
    assert rows["1"]["alias"] == "main" and rows["1"]["writable"] and rows["1"]["role"] == "own"
    assert rows["2"]["sandbox"] and not rows["2"]["writable"] and rows["9"]["role"] == "shared"


def test_find_ranks_by_name_and_dates_and_redacts_people_who_may_be_living(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {})
    year = __import__("datetime").date.today().year
    rows = [{"PersonId": 1, "FullName": "Mary Smith", "GivenName": "Mary", "Surname": "Smith", "BirthYear": 1900, "DeathYear": 1970},
            {"PersonId": 2, "FullName": "Mary Smyth", "GivenName": "Mary", "Surname": "Smyth", "BirthYear": 1950, "DeathYear": 2000},
            {"PersonId": 3, "FullName": "Mary Smith", "GivenName": "Mary", "Surname": "Smith", "BirthYear": year - 30, "DeathYear": None},
            {"PersonId": 4, "FullName": "Mary Smith", "GivenName": "Mary", "Surname": "Smith", "BirthYear": 1899, "DeathYear": None}]
    _fake_session(monkeypatch, {f"/api/person-picker/suggest/{TREE}": rows})
    out = discovery.find(tree_id=TREE, given="Mary", surname="Smith", birth=1900)
    ids = [r["person_id"] for r in out["results"]]
    assert ids[0] == "1" and out["total"] == 4 and out["truncated"] is False
    r3 = next(r for r in out["results"] if r["person_id"] == "3")
    assert r3["possibly_living"] and r3["name"] == "Living person (redacted)" and r3["birth_year"] is None
    assert next(r for r in out["results"] if r["person_id"] == "4")["name"] == "Mary Smith"      # unknown death but born long ago
    shown = discovery.find(tree_id=TREE, given="Mary", surname="Smith", include_living=True)
    assert next(r for r in shown["results"] if r["person_id"] == "3")["name"] == "Mary Smith"
    assert all(r["tree_id"] == str(TREE) and isinstance(r["person_id"], str) for r in out["results"])
    assert discovery.find(tree_id=TREE, limit=1, given="Mary")["truncated"] is True
    assert discovery.find(tree_id=TREE)["classification"] == "usage-error"


# ---------------------------------------------------------------- CLI wiring
def test_cli_resolves_tree_aliases_and_annotates_failures(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch, {"trees": {"sb": TREE}, "sandbox_trees": [TREE]})
    assert cli.main(["write", "fact-remove", "--tree", "sb", "--person", str(PERSON), "--assertion", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["classification"] == "dry-run"
    with pytest.raises(SystemExit) as e:
        cli.main(["write", "fact-remove", "--tree", "nope", "--person", "1"])
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 2 and out["classification"] == "unknown-tree" and out["needs_human"] is False and out["hint"]
    monkeypatch.setattr("ancestry_cli.ops.write", lambda **k: {"ok": False, "classification": "session-expired"})
    assert cli.main(["write", "fact-remove", "--tree", "sb", "--person", "1", "--assertion", "5", "--live"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["needs_human"] is True and out["retryable"] is False and "Sign in" in out["hint"]


def test_cli_profile_flag_selects_the_profile(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch, {"profiles": {"a": {"trees": {"t": 5}}, "b": {"trees": {"t": 6}}}})
    monkeypatch.setenv("ANCESTRY_CLI_PROFILE", "a")   # restored at teardown, so --profile does not leak into later tests
    monkeypatch.delenv("ANCESTRY_CLI_PROFILE", raising=False)
    cli.main(["--profile", "b", "write", "fact-remove", "--tree", "t", "--person", "1", "--assertion", "5"])
    out = json.loads(capsys.readouterr().out)
    assert "/tree/6/" in out["path"]


def test_lane_reset_needs_exactly_one_page(lane, monkeypatch):
    class B:
        def _cdp_json(self, base, path):
            return [{"type": "page", "id": "a", "url": "x"}, {"type": "page", "id": "b", "url": "y"}]
    with pytest.raises(rt.LaneError) as e:
        rt.lane_reset(B())
    assert e.value.code == "browser-lane-ambiguous"


def test_lock_file_override_shares_a_workspace_lock(monkeypatch, tmp_path):
    shared = tmp_path / "locks" / "ancestry.lock"
    monkeypatch.setenv("ANCESTRY_CLI_LOCK_FILE", str(shared))
    monkeypatch.setattr(rt, "breaker_until", lambda: None)
    assert config.lock_file() == shared
    with rt.lock():
        pass
    assert float(shared.read_text()) > 0  # same timestamp format as the workspace service_lock
