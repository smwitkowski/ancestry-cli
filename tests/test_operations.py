import json
import pytest
import ancestry_cli.runtime as rt
from ancestry_cli import ops as tw, cli

TREE, PERSON = 111111111, 555555555555


def test_fact_add_matches_har_shape():
    r = tw.build("fact-add", tree_id=TREE, person_id=PERSON, eventType="birth", date="01 Jan 1900")
    assert r["method"] == "POST" and r["path"].endswith("/assertion/0/save")
    assert sorted(r["body"]) == sorted(["assertionId", "date", "description", "eventType", "gender",
                                        "location", "name", "preferred", "showMap", "showOnLifeStory", "title"])
    assert r["body"]["preferred"] is None and r["body"]["assertionId"] == "0"


def test_fact_remove_is_side_effect_get():
    r = tw.build("fact-remove", tree_id=TREE, person_id=PERSON, assertion_id=700000000002)
    assert (r["method"], r["body"]) == ("GET", None) and r["path"].endswith("/700000000002/delete")


def test_person_remove_and_relative_add():
    assert tw.build("person-remove", tree_id=TREE, person_id=PERSON, name="ZZTEST X")["body"] == {"name": "ZZTEST X"}
    r = tw.build("relative-add", tree_id=TREE, person_id=PERSON, relation="Father", name_id=1, gender_id=2, status="Deceased")
    assert r["path"].endswith("/addperson") and r["body"]["type"] == "Father"
    assert r["body"]["values"]["radioTab"] == "New person"


@pytest.mark.parametrize("op,kw", [("fact-add", {}), ("fact-edit", {"eventType": "birth"}),
                                   ("relative-add", {"relation": "Cousin", "name_id": 1, "gender_id": 2}),
                                   ("fact-add", {"eventType": "x", "bogus": 1})])
def test_bad_requests_rejected(op, kw):
    with pytest.raises(tw.WriteRequestError):
        tw.build(op, tree_id=TREE, person_id=PERSON, **kw)


def test_gate(monkeypatch):
    ok = tw.write(op="fact-remove", tree_id=TREE, person_id=PERSON, assertion_id=5)
    assert ok["classification"] == "dry-run" and ok["dispatch_attempted"] is False
    assert tw.write(op="fact-remove", tree_id=222222222, person_id=PERSON, assertion_id=5)["ok"] is True   # dry-run on any tree
    unknown = tw.write(op="not-an-op", tree_id=TREE, person_id=PERSON, dry_run=False)
    assert unknown["ok"] is False and unknown["dispatch_attempted"] is False


def test_cli_dry_run(monkeypatch, capsys):
    rc = cli.main(["write", "fact-add", "--tree", str(TREE), "--person", str(PERSON),
                   "--set", "eventType=birth", "--set", "description=ZZTEST"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["classification"] == "dry-run" and "ZZTEST" not in json.dumps(out)


def test_new_har_verified_ops():
    r = tw.build("source-create", tree_id=TREE, person_id=PERSON, title="ZZTEST source")
    assert r["path"].endswith(f"/tree/{TREE}/source") and r["body"] == {"title": "ZZTEST source"}
    r = tw.build("citation-add", tree_id=TREE, person_id=PERSON, title="d", source_id=5)
    assert r["path"].endswith("/citation") and sorted(r["body"]) == ["sourceId", "title", "url"]
    r = tw.build("fact-attach-source", tree_id=TREE, person_id=PERSON, assertion_id=9, citation_id=7)
    assert r["path"].endswith("/9/attachSource") and sorted(r["body"]) == ["databaseId", "recordId", "sourceCitationId"]
    assert tw.build("fact-detach-source", tree_id=TREE, person_id=PERSON, assertion_id=9, citation_id=7)["path"].endswith("/detachSource")
    r = tw.build("weblink-add", tree_id=TREE, person_id=PERSON, href="https://example.com", title="t")
    assert r["path"].endswith("/weblinkadd") and sorted(r["body"]) == ["webLinkHref", "webLinkTitle"]
    r = tw.build("weblink-remove", tree_id=TREE, person_id=PERSON, web_link_id="abc")
    assert (r["method"], r["query"]) == ("GET", {"webLinkId": "abc"})
    for op in ("relative-add",):
        for rel in ("Father", "Mother", "Spouse"):
            assert tw.build(op, tree_id=TREE, person_id=PERSON, relation=rel, name_id=1, gender_id=2, status="Deceased")["body"]["type"] == rel


def test_name_edit_is_a_fact_save():
    r = tw.build("fact-edit", tree_id=TREE, person_id=PERSON, assertion_id=3, eventType="Name",
                 name={"givenName": "ZZTEST", "surname": "CAPTURE 21"})
    assert r["path"].endswith("/3/save") and r["body"]["name"]["surname"] == "CAPTURE 21"


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class _Bridge:
    """Records requests; one tooling page on a granted tree."""
    ACTOR = "00000000-0000-0000-0000-000000000099"

    def __init__(self, post_body=None):
        self.calls = []
        self.post_body = post_body if post_body is not None else {"success": True, "returnUrl": "SECRET"}
        outer = self

        class ChromeBrowserSession:
            def __init__(self, cdp_url=None, target_id=None):
                self.cdp_url, self.target_id = cdp_url, target_id

            def get(self, url, **kw):
                outer.calls.append(("GET", url.split("www.ancestry.com")[1], kw.get("writes_ok")))
                pr = {"PersonFullName": "ZZTEST A", "IsPersonLiving": False, "PersonSources": [], "PersonWebLinks": [],
                      "PersonFamily": {}, "PersonFacts": [{"AssertionId": "5", "Type": 74, "TypeString": "Residence",
                                                           "Title": "t", "Date": "1901", "Place": "", "Description": "d"}]}
                return _Resp(f"x /user/{outer.ACTOR}/tree/{TREE}/person/{PERSON} y "
                             '<script id="person-data">' + json.dumps({"person": {"PersonResearch": pr}}) + "</script>")

            def request(self, method, url, **kw):
                outer.calls.append((method, url.split("www.ancestry.com")[1], kw.get("params")))
                return _Resp('{\\"nameId\\":\\"11\\",\\"genderId\\":\\"22\\"}'.replace("\\\\", "\\"))

            def post(self, url, **kw):
                outer.calls.append(("POST", url.split("www.ancestry.com")[1], json.loads(kw["data"])))
                return _Resp(json.dumps(outer.post_body))
        self.ChromeBrowserSession = ChromeBrowserSession

    def _cdp_json(self, base, path):
        return [{"type": "page", "id": "T1", "url": f"https://www.ancestry.com/family-tree/person/tree/{TREE}/person/{PERSON}/facts"}]


def test_live_send_uses_actor_and_hides_values(monkeypatch):
    import contextlib
    from ancestry_cli import sender as live
    monkeypatch.setattr(rt, "lock", lambda s="ancestry": contextlib.nullcontext())
    b = _Bridge()
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON, bridge=b, name="ZZTEST X")
    assert out["ok"] and out["classification"] == "sent" and out["dispatch_attempted"]
    method, path, body = b.calls[-1]
    assert method == "POST" and "/user" not in path and path.endswith("/removePerson") and body == {"name": "ZZTEST X"}
    assert "SECRET" not in json.dumps(out) and out["response_shape"] == {"success": "bool", "returnUrl": "str"}


def test_live_relative_add_reads_anchor_ids(monkeypatch):
    import contextlib
    from ancestry_cli import sender as live
    monkeypatch.setattr(rt, "lock", lambda s="ancestry": contextlib.nullcontext())
    b = _Bridge(post_body={"newPid": "555"})
    out = live.send(op="relative-add", tree_id=TREE, person_id=PERSON, bridge=b,
                          relation="Father", given="ZZTEST", surname="CAPTURE 30", status="Deceased")
    assert out["ok"], out
    body = b.calls[-1][2]
    assert body["type"] == "Father" and body["values"]["nameId"] == "11" and body["values"]["genderId"] == "22"
    assert b.calls[-2][2] == {"rel": "father"}


def test_live_rejects_real_tree_and_non_live_ops():
    from ancestry_cli import sender as live
    assert live.send(op="fact-remove", tree_id=222222222, person_id=PERSON, assertion_id=5)["ok"] is False
    assert live.send(op="not-an-op", tree_id=TREE, person_id=PERSON)["classification"] == "configuration-error"


def test_confirmation_is_required_outside_sandbox_trees():
    from ancestry_cli import config
    assert config.sandbox_trees() == frozenset({111111111})
    assert config.needs_confirmation(222222222, None) and config.needs_confirmation(222222222, 999)
    assert not config.needs_confirmation(222222222, 222222222) and not config.needs_confirmation(111111111, None)
    assert config.tree_allowed(5) and not config.tree_allowed(0) and not config.tree_allowed("5")

def test_journal_records_inverse_and_undo(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("relative-add", TREE, PERSON, {"given": "ZZTEST", "surname": "CAPTURE 40"}, {"newPid": "555"})
    tj.record("fact-add", TREE, 555, {}, {"AttributeIds": ["777"]})
    tj.record("fact-edit", TREE, 555, {"assertion_id": "777"}, {"AttributeIds": ["777"]})  # no inverse
    assert [r["op"] for r in tj.outstanding()] == ["relative-add", "fact-add"]
    sent = []
    monkeypatch.setattr(tw, "write", lambda **kw: sent.append(kw) or {"ok": True, "status": 200})
    out = tj.command(action="undo", dry_run=False, all_entries=True)
    assert out["ok"] and [s["op"] for s in sent] == ["fact-remove", "person-remove"]  # newest first
    assert sent[0]["assertion_id"] == "777" and "name" not in sent[1] and sent[1]["person_id"] == 555   # no names in the journal
    assert tj.outstanding() == []
    assert tj.command(action="list")["outstanding"] == []


def test_error_code_is_surfaced_and_fails_the_send():
    from ancestry_cli import sender as live
    assert live._ids({"ErrorCode": 3}) == {"ErrorCode": 3} and live._ids({"gid": {"v": "123:4:5"}}) == {"gid": "123"}


def test_named_reads_map_to_inventory_endpoints():
    from ancestry_cli import reads as tr

    def named(action, **kw):
        base = dict(given=None, surname=None, birth=None, death=None, location=None, collection=None, record_id=None,
                    counts=False, tree_id=None, person_id=None)
        return tr._named(action, **{**base, **kw})

    assert named("search", given="Mary Ann", surname="Smith", birth="1900") == (
        "search_results", {}, {"name": "Mary+Ann_Smith", "priority": "usa", "searchMode": "advanced", "birth": "1900"})
    assert named("search", surname="Smith", collection=6224)[0] == "collection_results"
    assert named("search", surname="Smith", counts=True)[0] == "hit_counts"
    assert named("hints", tree_id=1, person_id=2)[2]["personId"] == "2"
    assert named("record", collection=5, record_id=6)[1] == {"collectionId": "5", "recordId": "6"}
    assert named("search") is None
    assert tr.read(action="hints", tree_id=None)["classification"] == "missing-arguments"


def test_hint_ops_are_query_only_posts():
    r = tw.build("hint-ignore", tree_id=TREE, person_id=PERSON, hint_id=77)
    assert (r["method"], r["body"], r["path"]) == ("POST", None, "/hintsui-personhints/api/IgnoreHint")
    assert r["query"]["hintId"] == "77" and r["query"]["treeId"] == str(TREE)
    assert tw.build("hint-restore", tree_id=TREE, person_id=PERSON, hint_id=77)["path"].endswith("/DeferHint")


def test_citation_remove_is_bodyless_post():
    r = tw.build("citation-remove", tree_id=TREE, person_id=PERSON, citation_id=99)
    assert (r["method"], r["body"]) == ("POST", None) and r["path"].endswith("/citation/99/removecitation")



def test_manual_cleanup_excludes_entries_marked_undone(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("hint-accept", TREE, PERSON, {"hint_id": "1"}, {"hintId": "1"})
    assert [r["op"] for r in tj.manual()] == ["hint-accept"]
    tj._append({"undo_of": 0})
    assert tj.manual() == []


def _live_env(monkeypatch):
    import contextlib
    from ancestry_cli import sender as live
    monkeypatch.setattr(rt, "lock", lambda s="ancestry": contextlib.nullcontext())
    return live


import pytest


@pytest.mark.parametrize("op,fields,body,outcome", [
    ("person-remove", {"name": "ZZTEST X"}, {"success": False}, "failed"),           # an explicit "no"
    ("person-remove", {"name": "ZZTEST X"}, {}, "unknown"),                          # ambiguous: it may have been applied
    ("fact-add", {"eventType": "Residence"}, {"status": False}, "failed"),
    ("fact-add", {"eventType": "Residence"}, {"AttributeIds": ["1"]}, "unknown"),
    ("fact-remove", {"assertion_id": 5}, [False], "unknown"),
    ("fact-attach-source", {"assertion_id": 5, "citation_id": 7}, {"ErrorCode": 3}, "failed"),
    ("relative-add", {"relation": "Father", "status": "Deceased"}, {}, "unknown"),
])
def test_http_200_with_a_failure_body_is_not_success(monkeypatch, tmp_path, op, fields, body, outcome):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    live = _live_env(monkeypatch)
    out = live.send(op=op, tree_id=TREE, person_id=PERSON, bridge=_Bridge(post_body=body), **fields)
    assert out["dispatch_attempted"] is True and out["ok"] is False
    assert tj._rows()[-1]["outcome"] == outcome                          # journaled either way
    if outcome == "failed":
        assert out["classification"] == "send-failed" and out["state"] == "unchanged"
    else:                                                                 # ambiguous: tell the caller which journal row to verify
        assert out["classification"] == "unknown-outcome" and out["state"] == "unknown" and out["journal_id"] == tj._rows()[-1]["id"]


def test_journal_failure_does_not_hide_a_successful_write(monkeypatch, tmp_path):
    from ancestry_cli import journal as tj
    live = _live_env(monkeypatch)
    monkeypatch.setattr(tj, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = live.send(op="person-remove", tree_id=TREE, person_id=PERSON,
                          bridge=_Bridge(), name="ZZTEST X")
    assert out["ok"] is True and out["journal_error"] == "OSError"


def test_exception_after_send_is_journaled_as_unknown(monkeypatch, tmp_path):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    live = _live_env(monkeypatch)
    b = _Bridge()
    monkeypatch.setattr(live, "succeeded", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = live.send(op="relative-add", tree_id=TREE, person_id=PERSON, bridge=b, relation="Father", status="Deceased")
    assert out["ok"] is False and out["dispatch_attempted"] is True
    assert [r["outcome"] for r in tj._rows()] == ["unknown"] and [r["op"] for r in tj.manual()] == ["relative-add"]


def test_real_trees_need_confirm_tree_for_live_writes(monkeypatch):
    live = _live_env(monkeypatch)
    sent = []
    monkeypatch.setattr(live, "send", lambda **k: sent.append(k) or {"ok": True})
    kw = dict(op="fact-remove", person_id=PERSON, assertion_id=5, dry_run=False)
    assert tw.write(tree_id=222222222, **kw)["classification"] == "confirm-tree-required"
    assert tw.write(tree_id=222222222, confirm_tree=999, **kw)["classification"] == "confirm-tree-required"
    assert sent == []
    assert tw.write(tree_id=222222222, confirm_tree=222222222, **kw)["ok"] is True and len(sent) == 1
    assert tw.write(tree_id=111111111, **kw)["ok"] is True      # a test tree needs no confirmation
    assert tw.write(tree_id=222222222, op="fact-remove", person_id=PERSON, assertion_id=5)["classification"] == "dry-run"


def test_tree_read_refuses_reads_that_start_server_work():
    from ancestry_cli import reads as tr
    class B:
        ENDPOINTS = [{"method": "GET", "name": "start_or_get_export", "group": "export", "path": "/x"}]
    assert tr.read(action="get", name="start_or_get_export", bridge=B())["classification"] == "side-effecting-read"


def test_hint_state_ops_are_text_plain_patches():
    for op, state in (("hint-maybe", "deferred"), ("hint-no", "rejected"), ("hint-new", "pending")):
        r = tw.build(op, tree_id=TREE, person_id=PERSON, hint_id=77)
        assert (r["method"], r["body"], r["text_plain"]) == ("PATCH", {"state": state}, True)
        assert r["path"] == f"/api/hintsui-api/trees/{TREE}/persons/{PERSON}/hints/77"
    assert tw.succeeded("hint-no", 200, {}) and not tw.succeeded("hint-no", 200, None)


def test_media_upload_dry_run_validates_the_file_and_media_remove_builds(tmp_path):
    import struct, zlib
    def png(w, h):
        raw = b"".join(b"\x00" + b"\xc8\x28\x28" * w for _ in range(h))
        ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
    f = tmp_path / "t.png"; f.write_bytes(png(8, 4))
    r = tw.build("media-upload", tree_id=TREE, person_id=PERSON, file=str(f), title="ZZTEST media")
    assert r["path"].endswith(f"/tree/{TREE}/person/{PERSON}/media") and r["upload"]["bytes"] == f.stat().st_size
    d = r["body"][0]["additionalFileDetails"]
    assert (d["fileHeight"], d["fileHWidth"], r["body"][0]["mimeType"]) == (4, 8, "image/png")
    for bad in (str(tmp_path / "missing.png"), "relative.png", str(tmp_path / "x.exe")):
        with pytest.raises(tw.WriteRequestError):
            tw.build("media-upload", tree_id=TREE, person_id=PERSON, file=bad, title="t")
    uid = "00000000-0000-4000-8000-000000000002"
    rm = tw.build("media-remove", tree_id=TREE, person_id=PERSON, media_id=uid)
    assert (rm["method"], rm["path"], rm["body"]) == ("DELETE", f"/api/media/viewer/api/trees/{TREE}/media/{uid}", None)
    with pytest.raises(tw.WriteRequestError):
        tw.build("media-remove", tree_id=TREE, person_id=PERSON, media_id="not-a-uuid")


def test_media_upload_journals_hash_and_undoes_with_media_remove(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("media-upload", TREE, PERSON, {}, {"mediaId": "00000000-0000-4000-8000-000000000002", "sha256": "ab", "bytes": 5, "file": "t.png"})
    row = tj.outstanding()[0]
    assert row["undo"]["op"] == "media-remove" and row["ids"]["sha256"] == "ab" and row["ids"]["file"] == "t.png"


def test_a_removal_resolves_the_creation_it_reverses_even_when_done_by_hand(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("relative-add", TREE, PERSON, {"given": "ZZTEST", "surname": "C"}, {"newPid": "555"})
    tj.record("fact-add", TREE, 555, {}, {"AttributeIds": ["777"]})
    tj.record("weblink-add", TREE, 555, {}, {"webLinkId": "w1"})
    tj.record("media-upload", TREE, 555, {}, {"mediaId": "m1"})
    assert len(tj.outstanding()) == 4
    tj.record("fact-remove", TREE, 555, {"assertion_id": "777"}, {})
    tj.record("weblink-remove", TREE, 555, {"web_link_id": "w1"}, {})
    tj.record("media-remove", TREE, 555, {"media_id": "m1"}, {})
    assert [r["op"] for r in tj.outstanding()] == ["relative-add"]
    tj.record("person-remove", TREE, 555, {"name": "x"}, {})
    assert tj.outstanding() == []
    tj.record("person-remove", 999, 556, {"name": "y"}, {}, outcome="failed")      # a failed removal resolves nothing
    tj.record("relative-add", 999, PERSON, {}, {"newPid": "556"})
    tj.record("person-remove", 999, 556, {"name": "y"}, {}, outcome="failed")
    assert [r["op"] for r in tj.outstanding()] == ["relative-add"]


def test_undo_needs_an_explicit_id_or_all(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("relative-add", TREE, PERSON, {"given": "Z", "surname": "Z"}, {"newPid": "9"})
    assert tj.command(action="undo", dry_run=False)["classification"] == "specify-id-or-all"
    assert tj.command(action="undo", dry_run=True, entry_id=0)["results"][0]["undo_op"] == "person-remove"
    assert len(tj.command(action="undo", dry_run=True, all_entries=True)["results"]) == 1


def test_snapshot_restore_and_diff(tmp_path, monkeypatch):
    from ancestry_cli import snapshots as ts, journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    monkeypatch.setenv("ANCESTRY_CLI_SNAPSHOTS", str(tmp_path / "snaps"))
    def page(date, desc):
        pr = {"PersonFullName": "ZZTEST A", "IsPersonLiving": False, "PersonSources": [], "PersonWebLinks": [], "PersonFamily": {},
              "PersonFacts": [{"AssertionId": "5", "Type": 74, "TypeString": "Residence", "Title": "t", "Date": date,
                               "Place": "Springfield, Illinois, USA", "Description": desc, "SourceCitationIDs": "9"},
                              {"AssertionId": "6", "Type": 44, "TypeString": "Gender", "Value": "Male"}]}
        return '<script id="person-data">' + json.dumps({"person": {"PersonResearch": pr}}) + "</script>"
    before = ts.snapshot_from_page(page("1901", "old"), 1, 2)
    after = ts.snapshot_from_page(page("1902", "old"), 1, 2)
    assert ts.diff(before, after) == {"added": [], "removed": [], "changed": {"5": ["Date"]}, "name_changed": False}
    path = ts.save(before, "fact-edit")
    assert oct(__import__("os").stat(path).st_mode)[-3:] == "600"
    r = ts.restore_fields(before, "5")
    assert r == {"eventType": "Residence", "date": "1901", "description": "old", "gender": "Male",
                 "location": {"placeName": "Springfield, Illinois, USA", "GPID": "", "showUnderline": False}}
    tj.record("fact-edit", TREE, PERSON, {"assertion_id": "5"}, {}, snapshot=path)
    u = tj.outstanding()[0]["undo"]
    assert u["op"] == "fact-edit" and u["assertion_id"] == "5" and u["fields"]["date"] == "1901"
    tj.record("fact-remove", TREE, PERSON, {"assertion_id": "5"}, {}, snapshot=path)
    assert tj.outstanding()[-1]["undo"]["op"] == "fact-add"
    assert tw.build("fact-edit", tree_id=TREE, person_id=PERSON, assertion_id=5, **r)["body"]["location"]["placeName"].startswith("Springfield")


def test_fact_edit_keeps_unspecified_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    live = _live_env(monkeypatch)
    pr = {"PersonFullName": "ZZTEST A", "IsPersonLiving": False, "PersonSources": [], "PersonWebLinks": [], "PersonFamily": {},
          "PersonFacts": [{"AssertionId": "5", "Type": 74, "TypeString": "Residence", "Title": "t", "Date": "1901",
                           "Place": "Springfield, Illinois, USA", "Description": "old"},
                          {"AssertionId": "6", "Type": 44, "TypeString": "Gender", "Value": "Male"}]}
    b = _Bridge(post_body={"status": True, "AttributeIds": ["5"]})
    page = f"x /user/{b.ACTOR}/tree/{TREE}/person/{PERSON} " + '<script id="person-data">' + json.dumps({"person": {"PersonResearch": pr}}) + "</script>"
    b.ChromeBrowserSession.get = lambda self, url, **kw: (b.calls.append(("GET", url.split("www.ancestry.com")[1], kw.get("writes_ok"))), _Resp(page))[1]
    out = live.send(op="fact-edit", tree_id=TREE, person_id=PERSON, bridge=b,
                          assertion_id="5", description="new")        # only the description is given
    assert out["ok"], out
    sent = [c for c in b.calls if c[0] == "POST"][-1][2]
    assert sent["description"] == "new" and sent["date"] == "1901" and sent["location"]["placeName"] == "Springfield, Illinois, USA"
    assert sent["eventType"] == "Residence" and sent["gender"] == "Male"
    missing = live.send(op="fact-edit", tree_id=TREE, person_id=PERSON, bridge=b, assertion_id="99", description="x")
    assert missing["classification"] == "fact-not-found" and missing["dispatch_attempted"] is False


def test_removing_a_person_resolves_everything_journaled_against_them(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("relative-add", TREE, PERSON, {"given": "Z", "surname": "Z"}, {"newPid": "555"})
    tj.record("fact-add", TREE, 555, {}, {"AttributeIds": ["7"]})
    tj.record("fact-edit", TREE, 555, {"assertion_id": "7"}, {})
    tj.record("person-remove", TREE, 555, {"name": "x"}, {})
    assert tj.outstanding() == []


def test_tag_ops_build_resolve_and_journal(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj, tags as tree_tags
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    r = tw.build("tag-add", tree_id=TREE, person_id=PERSON, tags="To Do, 34")
    assert r["path"].endswith("/addtags") and r["body"] == {"tagnames": ["33", "34"]}
    assert tw.build("tag-remove", tree_id=TREE, person_id=PERSON, tags=["33"])["path"].endswith("/removetags")
    assert tree_tags.resolve_tags("brick wall, VERIFIED") == ["4", "24"] and len(tree_tags.TAG_IDS) == 79
    for bad in ("Not A Tag", "", "12345678"):
        with pytest.raises(tw.WriteRequestError):
            tw.build("tag-add", tree_id=TREE, person_id=PERSON, tags=bad)
    assert tw.succeeded("tag-add", 200, {"statusCode": 200, "addedTags": ["33"]})
    assert not tw.succeeded("tag-add", 200, {"statusCode": 200, "addedTags": []})
    assert tw.succeeded("tag-remove", 200, {"statusCode": 200, "removedTags": ["33"]})
    tj.record("tag-add", TREE, PERSON, {"tags": "To Do"}, {"addedTags": ["33"]})
    assert tj.outstanding()[0]["undo"] == {"op": "tag-remove", "person_id": PERSON, "fields": {"tags": ["33"]}}
    tj.record("tag-remove", TREE, PERSON, {"tags": ["To Do"]}, {"removedTags": ["33"]})   # names in, ids out
    assert tj.outstanding() == []


def test_note_set_builds_succeeds_and_restores_the_previous_note(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    r = tw.build("note-set", tree_id=TREE, person_id=PERSON, text="hello")
    assert r["path"].endswith("/savePersonNotes") and r["body"] == {"note": "hello"}
    assert tw.build("note-set", tree_id=TREE, person_id=PERSON, text="")["body"] == {"note": ""}     # clearing is allowed
    with pytest.raises(tw.WriteRequestError):
        tw.build("note-set", tree_id=TREE, person_id=PERSON)
    assert tw.succeeded("note-set", 200, {"id": "1", "txt": "<line>x</line>"}) and not tw.succeeded("note-set", 200, {})
    snap = tmp_path / "s.json"; snap.write_text(json.dumps({"note_before": "old note"}))
    tj.record("note-set", TREE, PERSON, {}, {}, snapshot=str(snap))
    assert tj.outstanding()[0]["undo"] == {"op": "note-set", "person_id": PERSON, "fields": {"text": "old note"}}


def test_status_has_no_default_and_success_checks_are_strict(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    with pytest.raises(tw.WriteRequestError):
        tw.build("relative-add", tree_id=TREE, person_id=PERSON, relation="Father", name_id=1, gender_id=2)   # status missing
    assert tw.succeeded("media-remove", 200, {}) and not tw.succeeded("media-remove", 200, None)   # non-JSON 200 is not success
    for op in ("hint-ignore", "hint-no", "fact-detach-source"):
        assert tw.succeeded(op, 200, {}) and not tw.succeeded(op, 200, None) and not tw.succeeded(op, 200, {"error": 1})
    # journal matching is exact: one hint-new resolves only its own hint, a detach only its own citation
    tj.record("hint-no", TREE, PERSON, {"hint_id": "1"}, {"hintId": "1"})
    tj.record("hint-maybe", TREE, PERSON, {"hint_id": "2"}, {"hintId": "2"})
    tj.record("hint-new", TREE, PERSON, {"hint_id": "1"}, {"hintId": "1"})
    assert [r["ids"]["hintId"] for r in tj.outstanding() if r["op"] in ("hint-no", "hint-maybe")] == ["2"]
    tj.record("fact-attach-source", TREE, PERSON, {"assertion_id": "5", "citation_id": "A"}, {})
    tj.record("fact-attach-source", TREE, PERSON, {"assertion_id": "5", "citation_id": "B"}, {})
    tj.record("fact-detach-source", TREE, PERSON, {"assertion_id": "5", "citation_id": "A"}, {})
    assert [r["citation_id"] for r in tj.outstanding() if r["op"] == "fact-attach-source"] == ["B"]


def test_undo_all_skips_real_tree_entries(tmp_path, monkeypatch):
    from ancestry_cli import journal as tj
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    tj.record("relative-add", 222222222, PERSON, {"given": "Z", "surname": "Z"}, {"newPid": "9"})
    sent = []
    monkeypatch.setattr(tw, "write", lambda **kw: sent.append(kw) or {"ok": True})
    out = tj.command(action="undo", dry_run=False, all_entries=True)
    assert sent == [] and out["results"][0]["skipped"] == "real-tree-needs-explicit-id"
    out = tj.command(action="undo", dry_run=False, entry_id=0)
    assert len(sent) == 1 and out["ok"]


def test_every_operation_is_fully_described_and_buildable():
    from ancestry_cli import cli as _cli
    risks = {"additive", "edit", "structural", "destructive", "hint-state"}
    assert set(tw.OPS) == set(tw.SPECS) and len(tw.OPS) == 22
    for name, spec in tw.SPECS.items():
        assert spec.summary and spec.undo and spec.example and spec.risk in risks, name
        assert callable(spec.builder) and callable(spec.success), name
        d = spec.describe()
        assert d["op"] == name and isinstance(d["required"], list) and isinstance(d["optional"], list)
    # `ancestry ops` reports exactly the registry
    assert [o["op"] for o in _cli._ops()["operations"]] == list(tw.OPS)
    assert _cli._ops("fact-edit")["required"] == ["--assertion"]
    assert tw.succeeded("not-an-op", 200, {}) is False


def test_generated_docs_are_current():
    """docs/operations.md and docs/errors.md are generated from the code; fail if someone edits one and not the other."""
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_docs.py"
    spec = importlib.util.spec_from_file_location("gen_docs", script)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.main(check=True) == 0, "run: uv run python scripts/gen_docs.py"


def test_every_emitted_error_code_is_in_the_catalog():
    """Scan the source for the codes the tool can emit: each must be in runtime.ERRORS (so it has a hint and flags)."""
    import re
    from pathlib import Path
    from ancestry_cli import runtime
    patterns = (r'failure\(\s*"([a-z][a-z0-9\-]+)"', r'"classification":\s*"([a-z][a-z0-9\-]+)"', r'LaneError\(\s*"([a-z][a-z0-9\-]+)"',
                r'_Refused\("([a-z\-]+)"', r'WriteRequestError\("([a-z\-]+)"', r'classification="([a-z\-]+)"')
    emitted = set()
    for path in (Path(__file__).resolve().parents[1] / "src" / "ancestry_cli").glob("*.py"):
        for pattern in patterns:
            emitted |= set(re.findall(pattern, path.read_text()))
    success = {"dry-run", "sent", "read", "hints", "whoami", "trees", "find", "person", "ops", "doctor", "lane-reset", "journal",
               "verify", "resolved", "undone", "undo-dry-run", "endpoints", "accepted", "init"}
    missing = sorted(c for c in emitted - success if c not in runtime.ERRORS)
    assert not missing, f"codes emitted but not in runtime.ERRORS: {missing}"
    assert all(e.hint for e in runtime.ERRORS.values())


def test_failure_state_and_unknown_outcome_carry_a_journal_id():
    from ancestry_cli import runtime as _rt
    assert _rt.failure("x")["state"] == "unchanged" and _rt.failure("x", dispatched=True)["state"] == "unknown"
    assert _rt.failure("x", dispatched=True, state="changed")["state"] == "changed"
    out = _rt.annotate(_rt.failure("unknown-outcome", dispatched=True, journal_id=7))
    assert out["journal_id"] == 7 and out["retryable"] is False and "journal verify" in out["hint"]
