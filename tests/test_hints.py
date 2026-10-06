"""The hint accept flow against a fake browser: list, dry-run, live accept, unconfirmed upload, guards."""
import contextlib
import json

import pytest

import ancestry_cli.runtime as rt
from ancestry_cli import config, hints, journal as tj, merge_payload

TREE, PERSON, HINT = 111111111, 555555555556, "100000000001"
PAGE = json.dumps({"html": {"body": f'<div>{{"HintId":"{HINT}"}} recordGid="11729617:7667"</div>'
                                    '<div>{"HintId":"100000000002"} member tree hint</div>'},
                   "js": {"local": ["/hintsui-eval/1.0.0-455994a/app.js"]}})


class _Resp:
    def __init__(self, data, status=200):
        self.text, self.status_code = json.dumps(data) if not isinstance(data, str) else data, status


class _Bridge:
    def __init__(self, confirm=True, job_success=True):
        self.posts, self.confirm, self.job_success = [], confirm, job_success
        outer = self

        class ChromeBrowserSession:
            def __init__(self, cdp_url=None, target_id=None):
                pass

            def get(self, url, **kw):
                if "PersonHintsList" in url:
                    return _Resp(PAGE)
                if "comparison" in url:
                    return _Resp({"RecordNodes": {}})
                if url.endswith("/status"):
                    return _Resp({"pending": False, "success": outer.job_success, "location": "L"})
                raise AssertionError(url)

            def post(self, url, **kw):
                outer.posts.append(url.rsplit("/job/", 1)[1])
                if url.endswith("/upload"):
                    return _Resp({"pending": True, "location": "L", "postConfirmationPayload": {"merge": {}}})
                return _Resp({"success": outer.confirm})
        self.ChromeBrowserSession = ChromeBrowserSession

    def _cdp_json(self, base, path):
        return [{"type": "page", "id": "T1", "url": "https://www.ancestry.com/family-tree/trees"}]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"sandbox_trees": [TREE]}))
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    monkeypatch.setattr(rt, "lock", lambda s="ancestry": contextlib.nullcontext())
    monkeypatch.setattr(merge_payload, "_build_upload_body",
                        lambda *a, **k: {"Nodes": {"1:99": {"Events": [{"AssertionId": 1}, {}, {}], "Names": [1]}}})
    monkeypatch.setattr(merge_payload, "_build_confirmation_body", lambda **k: {"postConfirmationPayload": {"merge": {}}})


def test_parse_hints_pairs_each_hint_with_its_record():
    body = ('<div data-pubdata=\'{"HintId":"1000000001"}\'>recordGid:"555555555:7163"</div>'
            '<div data-pubdata=\'{"HintId":"1000000002"}\'>member tree hint, no record</div>'
            '<a href="x?hintId=1000000003">recordGid="666666666:2562"</a>')
    assert hints.parse_hints(body) == [
        {"hint_id": "1000000001", "record_id": "555555555", "collection_id": "7163"},
        {"hint_id": "1000000002", "record_id": None, "collection_id": None},
        {"hint_id": "1000000003", "record_id": "666666666", "collection_id": "2562"}]


def test_list_and_dry_run_send_nothing(env):
    b = _Bridge()
    out = hints.command(action="list", tree_id=TREE, person_id=PERSON, bridge=b)
    assert out["ok"] and [h["hint_id"] for h in out["hints"]] == [HINT, "100000000002"]
    out = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, bridge=b)
    assert out["classification"] == "dry-run" and out["preview"] == {
        "events_cited_existing": 1, "events_new_from_record": 2, "names_cited": 1, "cite_only": False}
    assert b.posts == []
    assert hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id="100000000002", bridge=b)["classification"] == "hint-not-found-or-not-a-record"
    assert hints.command(action="accept", tree_id=TREE, person_id=PERSON, bridge=b)["classification"] == "hint-id-required"


def test_live_accept_runs_the_three_steps_and_journals_it(env):
    b = _Bridge()
    out = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, bridge=b, dry_run=False, poll_seconds=1)
    assert out["ok"] and out["classification"] == "accepted" and b.posts == ["upload", "post/confirmation"]
    row = tj._rows()[-1]
    assert row["op"] == "hint-accept" and row["outcome"] == "ok" and row["ids"]["recordId"] == "11729617"
    again = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, bridge=_Bridge(), dry_run=False)
    assert again["classification"] == "duplicate-write"          # the same accept is on record


@pytest.mark.parametrize("confirm,job,classification", [(False, True, "confirmation-failed"), (True, False, "merge-job-failed")])
def test_an_unfinished_accept_is_an_unknown_outcome_not_a_failure(env, confirm, job, classification):
    out = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, dry_run=False, poll_seconds=1,
                        bridge=_Bridge(confirm=confirm, job_success=job))
    assert out["ok"] is False and out["classification"] == classification and out["dispatch_attempted"] is True
    assert tj._rows()[-1]["outcome"] == "unknown" and [r["id"] for r in tj.manual()] == [0]


def test_guards_apply_to_live_accepts(env, monkeypatch, tmp_path):
    out = hints.command(action="accept", tree_id=999, person_id=PERSON, hint_id=HINT, dry_run=False, bridge=_Bridge())
    assert out["classification"] == "confirm-tree-required"
    (tmp_path / "config.json").write_text(json.dumps({"sandbox_trees": [TREE], "write_trees": [TREE]}))
    out = hints.command(action="accept", tree_id=999, person_id=PERSON, hint_id=HINT, dry_run=False, confirm_tree=999, bridge=_Bridge())
    assert out["classification"] == "tree-not-writable"


def test_an_unfinished_accept_reports_its_journal_id_and_blocks_a_blind_retry(env):
    out = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, dry_run=False, poll_seconds=1,
                        bridge=_Bridge(confirm=False))
    assert out["classification"] == "confirmation-failed" and out["state"] == "unknown" and out["journal_id"] == 0
    assert tj._rows()[0]["req_hash"]                          # unknown rows carry the request hash...
    again = hints.command(action="accept", tree_id=TREE, person_id=PERSON, hint_id=HINT, dry_run=False, bridge=_Bridge())
    assert again["classification"] == "duplicate-write" and again["journal_id"] == 0        # ...so the guard refuses a repeat
