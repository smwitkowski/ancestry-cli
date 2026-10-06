from ancestry_cli import collections as col, config, reads

BODY = {"results": {"items": [
    {"Label": "Court, Land, Wills & Financial", "Token": "1|Category|CAT=36",
     "Collections": [{"Token": "1|Category|CAT=36;COL=63131", "Label": "Deeds Index", "Count": 10}]},
    {"Label": "Newspapers", "Token": "1|Category|CAT=38",
     "Collections": [{"Token": "1|Category|CAT=38;COL=63131", "Label": "Deeds Index", "Count": 10},
                     {"Token": "1|Category|CAT=38;COL=7", "Label": "Gazette", "Count": 99}]}]}}


def test_find_flattens_dedupes_sorts_and_remembers(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(reads, "read", lambda **kw: {"ok": True, "body": BODY})
    out = col.find(keyword="deed")
    assert [r["collection_id"] for r in out["collections"]] == [7, 63131]
    assert out["collections"][1]["also_in"] == ["Newspapers"]
    assert [r["label"] for r in col.listing(query="deeds")["collections"]] == ["Deeds Index"]


def test_find_needs_a_name_or_keyword():
    assert col.find()["classification"] == "missing-arguments"


def test_unknown_profile_is_an_error_not_a_fallback(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text('{"write_trees": [1], "profiles": {"a": {"write_trees": [2]}}}')
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("ANCESTRY_CLI_PROFILE", "typo")
    assert config.unknown_profile() == "typo"
    monkeypatch.setenv("ANCESTRY_CLI_PROFILE", "a")
    assert config.unknown_profile() is None
    monkeypatch.delenv("ANCESTRY_CLI_PROFILE")
    assert config.unknown_profile() is None          # no profile named: the flat config is the default


def test_source_delete_request_and_undo_of_source_create():
    from ancestry_cli import journal, ops
    r = ops.build("source-delete", tree_id=5, person_id=7, source_id="780832148", actor="g")
    assert r["method"] == "DELETE" and r["path"].endswith("/tree/5/source/780832148") and r["body"] is None
    inv = journal.inverse("source-create", 5, 7, {"title": "t"}, {"gid": "780832148"})
    assert inv == {"op": "source-delete", "person_id": 7, "fields": {"source_id": "780832148"}}


def test_source_delete_title_guard(monkeypatch):
    import json
    import pytest
    from ancestry_cli import sender

    class W:
        tree_id, actor = 5, "g"
        class inner:
            @staticmethod
            def request(*a, **k):
                return type("R", (), {"text": json.dumps({"title": "Real"})})()
    monkeypatch.setattr("ancestry_cli.journal._rows", lambda: [])
    with pytest.raises(sender._Refused) as e:
        sender._check_source_title(W, {"source_id": "9"})
    assert e.value.code == "source-title-required"
    with pytest.raises(sender._Refused) as e:
        sender._check_source_title(W, {"source_id": "9", "expect_title": "Other"})
    assert e.value.code == "source-title-mismatch"
    sender._check_source_title(W, {"source_id": "9", "expect_title": "Real"})
    monkeypatch.setattr("ancestry_cli.journal._rows", lambda: [{"op": "source-create", "tree_id": 5, "ids": {"gid": "9"}}, {"resolved": 1}])
    sender._check_source_title(W, {"source_id": "9"})


def test_source_and_citation_field_builders():
    from ancestry_cli import ops
    r = ops.build("source-create", tree_id=5, person_id=7, title="T", author="A", repository_id="9", actor="g")
    assert r["body"] == {"title": "T"} and r["then"]["method"] == "PUT" and r["then"]["path"].endswith("/source/{id}")
    assert r["then"]["body"] == {"title": "T", "auth": "A", "pub": "", "publ": "", "pubd": "", "cn": "", "note": "", "refn": "",
                                 "repositoryId": "9"}
    assert "then" not in ops.build("source-create", tree_id=5, person_id=7, title="T", actor="g")
    r = ops.build("citation-add", tree_id=5, person_id=7, title="T", source_id="3", date="1900", actor="g")
    assert r["then"]["body"] == {"title": "T", "url": "", "d": "1900", "oi": "", "trans": "", "sourceId": "3"}
    r = ops.build("citation-edit", tree_id=5, person_id=7, citation_id="4", title="T", source_id="3", transcription="x", actor="g")
    assert r["method"] == "PUT" and r["body"]["trans"] == "x"
    assert ops.build("repository-create", tree_id=5, person_id=7, name="R", address="a", actor="g")["body"]["adr"] == "a"


def test_apply_canary_stops_and_receipt(tmp_path, monkeypatch):
    import json
    from ancestry_cli import apply as ap, ops
    calls = []

    def fake(**kw):
        calls.append(kw)
        if kw["dry_run"]:
            return {"ok": True}
        return {"ok": True, "state": "changed", "classification": "sent", "journal_id": len(calls)}
    monkeypatch.setattr(ops, "write", fake)
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"tree": 5, "operations": [{"op": "fact-add", "person": 1, "set": {"a": "b"}} for _ in range(3)]}))
    out = ap.apply(manifest=str(m))
    assert out["classification"] == "apply-dry-run" and not [c for c in calls if not c["dry_run"]]
    out = ap.apply(manifest=str(m), live=True)
    assert out["ok"] and [r["index"] for r in json.loads(m.read_text())["receipt"]] == [0, 1, 2]
    live_calls = len([c for c in calls if not c["dry_run"]])
    assert ap.apply(manifest=str(m), live=True)["ok"] and len([c for c in calls if not c["dry_run"]]) == live_calls   # resume skips done steps


def test_image_crop_box():
    import pytest
    from ancestry_cli.images import _crop_box
    assert _crop_box("0.1,0.1,0.5,0.5", 1000, 500) == (100, 50, 600, 300)
    assert _crop_box("10,20,30,40", 1000, 500) == (10, 20, 40, 60)
    with pytest.raises(ValueError):
        _crop_box("900,0,200,10", 1000, 500)


def test_find_complete_pages_and_redacts(monkeypatch):
    import contextlib
    from ancestry_cli import discovery
    pages = {1: [{"gid": {"v": f"{i}:1:5"}, "Names": [{"g": "A", "s": "B"}], "Events": [{"t": "Birth", "nd": "1800"}]} for i in range(50)],
             2: [{"gid": {"v": "99:1:5"}, "Names": [{"g": "C", "s": "D"}], "Events": [], "l": True}]}
    monkeypatch.setattr(discovery, "session", lambda: contextlib.nullcontext((None, None, None)))
    monkeypatch.setattr(discovery, "get_json", lambda inner, path, params: pages.get(int(params["page"]), []))
    monkeypatch.setattr(discovery, "guarded", lambda fn: fn())
    out = discovery.find_complete(tree_id=5, surname="B", limit=100)
    assert out["total"] == 51 and out["complete"] is True
    assert out["results"][-1]["name"] != "D" and out["results"][-1]["possibly_living"] is True
