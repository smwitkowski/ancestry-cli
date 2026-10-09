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


def test_relative_link_body_and_no_self_link():
    import pytest
    from ancestry_cli import ops
    r = ops.build("relative-link", tree_id=5, person_id=7, relation="Father", existing_person_id="9", actor="g")
    assert r["body"]["values"]["apmFindExistingPerson"]["PID"] == 9 and r["body"]["type"] == "Father"
    with pytest.raises(ops.WriteRequestError):
        ops.build("relative-link", tree_id=5, person_id=7, relation="Father", existing_person_id="7", actor="g")


def test_match_tags():
    from ancestry_cli.match import tag
    assert tag({"exact": 0, "similar": 0, "different": 0}) == "attached"
    assert tag({"exact": 3, "similar": 1, "different": 0}) == "likely_match"
    assert tag({"exact": 1, "similar": 0, "different": 3}) == "conflict"
    assert tag({"exact": 0, "similar": 2, "different": 0}) == "namesake"
    assert tag({"exact": 2, "similar": 3, "different": 1}) == "maybe"


def test_newspapers_page_parse():
    from ancestry_cli.newspapers import parse
    html = ('<html><head><title>Daily - Lebanon | Page 2 archive - Newspapers.com™</title></head><body>'
            '<script>{"isLoggedIn":true,"isSubscriber":false}</script>Get access to this page with a Subscription! A Publisher Extra® Newspaper '
            '<a href="/image/55/?article=aaa">Funerals</a><a href="/image/55/?article=aaa&terms=Jo%20Doe">Jo Doe</a>'
            '<a href="/image/55/?article=bbb">Other</a> Show 39 more articles</body></html>')
    info = parse(html, "55")
    assert info["title"] == "Daily - Lebanon | Page 2 archive" and info["publisher_extra"] and info["page_gated"]
    assert info["logged_in"] and info["subscriber"] is False and info["more_articles"] == 39
    assert info["articles"][0] == {"article": "aaa", "headline": "Funerals", "names": ["Jo Doe"]}


def test_custom_event_body_needs_its_label():
    import pytest
    from ancestry_cli import ops
    b = ops.build("fact-add", tree_id=5, person_id=7, eventType="CustomEvent", label=" Church membership ", description="d", actor="g")["body"]
    assert b["eventType"] == "customevent" and b["customEventTitle"] == "Church membership" and b["description"] == "d"
    with pytest.raises(ops.WriteRequestError):
        ops.build("fact-add", tree_id=5, person_id=7, eventType="CustomEvent", description="d", actor="g")
    with pytest.raises(ops.WriteRequestError):
        ops.build("fact-add", tree_id=5, person_id=7, eventType="Residence", label="x", actor="g")
    from ancestry_cli import sender
    assert sender._explicitly_rejected(200, {"statusCode": 400}) and not sender._explicitly_rejected(200, {"statusCode": 200})


def test_census_rows_group_and_label_columns():
    from ancestry_cli import census
    panel = {"fieldLabels": [{"fieldName": "SelfRelationToHead", "labelText": "Relation to Head"}, {"fieldName": "SelfResidenceAge", "labelText": "Age"}],
             "records": [{"pid": 1, "householdId": "h1", "fullName": "A B", "recordFields": [{"fieldName": "SelfRelationToHead", "value": "Head"},
                                                                                          {"fieldName": "SelfResidenceAge", "value": "40", "correctedValue": "41"}]}]}
    rows, labels = census._rows(panel)
    assert labels == ["Relation to Head", "Age"] and rows[0]["fields"] == {"Relation to Head": "Head", "Age": "41"} and rows[0]["household_id"] == "h1"


def test_potential_parent_review_projection():
    from ancestry_cli import hints
    review = {"Info": {"Name": "Michael Ryan", "Gender": "m", "Birth": {"Date": "1813", "Location": "Thurles"}, "Death": {"Date": "1860", "Location": "Ontario"}},
              "Records": [{"Title": "Parish Registers", "Gid": "151:61039:1777061", "ImageId": "x", "Fields": [{"Label": "Name", "Value": "Michl Ryan"}]}],
              "Family": {"a": {"IsPrimaryNode": True, "Name": {"Record": {"Given": "Michael", "Surname": "Ryan"}},
                               "Family": {"Father": "f", "Mother": None, "Siblings": [], "FamilyUnits": [{"Wife": "w", "Children": ["c"]}]}},
                         "f": {"Name": {"Record": {"Given": "John", "Surname": "Ryan"}}}, "w": {"Name": {"Record": {"Given": "Ellen"}}},
                         "c": {"Name": {"Record": {"Given": "Pat", "Surname": "Ryan"}}}}}
    out = hints.parse_review(review, "father", {"HintId": "9", "SourceGid": "5:1030:77"})
    assert out["name"] == "Michael Ryan" and out["source_tree_id"] == "77" and out["records"][0]["collection_id"] == "61039"
    assert out["source_tree_family"] == {"parents": ["John Ryan"], "spouses": ["Ellen"], "children": ["Pat Ryan"], "siblings": []}
    living = hints.parse_review({"Info": {"Name": "Jo Doe", "Birth": {"Date": "1990"}}}, "mother", {"HintId": "1", "SourceGid": "1:1030:2"})
    assert living["possibly_living"] and living["name"] is None


def test_children_arrive_as_a_list_of_lists():
    from ancestry_cli import discovery
    assert discovery._flatten_members([[{"Id": 1}, {"Id": 2}], [{"Id": 3}]]) == [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    assert discovery._flatten_members([{"Id": 9}]) == [{"Id": 9}]


def test_son_and_daughter_use_the_sites_child_type_with_a_parent_set():
    from ancestry_cli import ops
    b = ops.build("relative-add", tree_id=5, person_id=7, relation="Son", status="Deceased", name_id="1", gender_id="2", father_id="9",
                  mother_id="7", actor="g")["body"]
    assert b["type"] == "Child" and b["values"]["genderRadio"] == "Male" and b["values"]["parentSet"] == {"fatherId": "9", "motherId": "7"}
    b = ops.build("relative-add", tree_id=5, person_id=7, relation="Sister", status="Living", name_id="1", gender_id="2", father_id="",
                  mother_id="3", actor="g")["body"]
    assert b["type"] == "Sister" and b["values"]["genderRadio"] == "Female" and b["values"]["parentSet"] == {"fatherId": "", "motherId": "3"}


def test_over_length_source_and_citation_text_is_refused_up_front():
    import pytest
    from ancestry_cli import ops
    with pytest.raises(ops.WriteRequestError) as e:
        ops.build("citation-add", tree_id=5, person_id=7, title="x" * 257, source_id="3", actor="g")
    assert e.value.problems == [{"field": "title", "issue": "too-long", "max": 256}]
    with pytest.raises(ops.WriteRequestError) as e:
        ops.build("source-create", tree_id=5, person_id=7, title="ok", publication_date="y" * 129, actor="g")
    assert e.value.problems[0]["field"] == "publication_date"
    ops.build("citation-add", tree_id=5, person_id=7, title="x" * 256, source_id="3", transcription="t" * 5000, actor="g")


def test_preloaded_state_is_extracted_from_a_search_page():
    import pytest
    from ancestry_cli import reads
    page = '<script>window.__PRELOADED_STATE__ = {"a": {"b": [1, 2]}, "c": "}"};</script><p>x</p>'
    assert reads._preloaded_state(page) == {"a": {"b": [1, 2]}, "c": "}"}
    with pytest.raises(ValueError):
        reads._preloaded_state("<html>none</html>")


def test_couple_terms_reach_the_collection_search():
    from ancestry_cli import reads
    name, path, params = reads._named("search", given="Michl", surname="Ryan", birth=None, death=None, location=None, collection=61039,
                                      record_id=None, counts=False, tree_id=None, person_id=None, spouse="Ellen_Greely")
    assert name == "collection_lane" and path == {"collectionId": "61039"} and params == {"name": "Michl_Ryan", "spouse": "Ellen_Greely"}


def test_relationship_remove_and_child_link_bodies():
    import pytest
    from ancestry_cli import ops
    r = ops.build("relationship-remove", tree_id=5, person_id=7, other="9", type="W", actor="g")
    assert r["path"].endswith("/person/7/relationship/9/removerelationship") and r["body"] == {"type": "W", "parentType": "F"}
    with pytest.raises(ops.WriteRequestError):
        ops.build("relationship-remove", tree_id=5, person_id=7, other="7", type="W", actor="g")
    with pytest.raises(ops.WriteRequestError):
        ops.build("relationship-remove", tree_id=5, person_id=7, other="9", type="X", actor="g")
    b = ops.build("relative-link", tree_id=5, person_id=7, relation="Son", existing_person_id="9", father_id="7", mother_id="3", actor="g")["body"]
    assert b["type"] == "Child" and b["values"]["apmFindExistingPerson"]["PID"] == 9
    assert b["values"]["parentSet"] == {"fatherId": "7", "motherId": "3"} and b["values"]["params"]["parentSet"] == b["values"]["parentSet"]
    assert b["values"]["relationModifier"] == 4 and b["values"]["reverseRelation"] == "f"
    spouse = ops.build("relative-link", tree_id=5, person_id=7, relation="Spouse", existing_person_id="9", actor="g")["body"]
    assert spouse["type"] == "Spouse" and "parentSet" not in spouse["values"]


def test_hint_card_content_duplicate_facts_and_journal_summary(tmp_path, monkeypatch):
    import json
    from ancestry_cli import discovery, hints, journal
    card = ('"HintId":"1111111111" <section class="hintCard" data-hintId="1111111111" data-databasecategory="Census &amp; Voter Lists" '
            'data-objectid="5" data-ube="{&quot;matchScore&quot;:543,&quot;numberOfNewAssertions&quot;:2,&quot;numberOfNewFamilyMembers&quot;:0}">'
            '<h2 class="hintTitle x"><a>1910 Census</a></h2><div>Residence</div><div>Brooklyn</div><button>Review</button></section>')
    h, = hints.parse_hints(card)
    assert (h["kind"], h["title"], h["match_score"], h["new_facts"], h["category"]) == ("record", "1910 Census", 543, 2, "Census & Voter Lists")
    assert "Residence | Brooklyn" in h["summary"]
    facts = [{"type": "Birth", "date": "1890", "place": "A", "preferred": True, "source_count": 1, "assertion_id": "1"},
             {"type": "Birth", "date": "1891", "place": "B", "preferred": False, "source_count": 0, "assertion_id": "2"},
             {"type": "Family Event", "date": "1940", "place": "", "preferred": True, "source_count": 0, "assertion_id": "3"},
             {"type": "Family Event", "date": "1940", "place": "", "preferred": True, "source_count": 0, "assertion_id": "4"}]
    dup, = discovery.duplicate_facts(facts)
    assert dup["type"] == "Birth" and dup["same_date"] is False and len(dup["facts"]) == 2
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    (tmp_path / "j.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"id": 0, "ts": "2026-10-08T01:00:00Z", "op": "fact-add", "tree_id": 1, "person_id": 9, "ids": {}, "outcome": "ok"},
        {"id": 1, "ts": "2026-10-09T01:00:00Z", "op": "relative-add", "tree_id": 1, "person_id": 9, "ids": {"newPid": 77}, "outcome": "ok"},
        {"id": 2, "ts": "2026-10-09T02:00:00Z", "op": "fact-detach-source", "tree_id": 1, "person_id": 9, "ids": {}, "outcome": "unknown"}]))
    out = journal.summary(since="2026-10-09")
    assert out["writes"] == 2 and out["new_people"][0]["person_id"] == "77" and out["unknown_outcomes"] == [2]
    assert out["detaches_and_removals"][0]["op"] == "fact-detach-source"


def test_geneteka_marriage_row_and_extras():
    from ancestry_cli import geneteka
    cell = ('<img src="images/i.png" title="&#013;Place: Zaremby " ><a href="http://x.example/ap" target="_blank"><img src="images/z.png" '
            'title="Miejsce przechowywania ksiąg: &#013;Archiwum &#013;Lomza"></a><a href="u"><img src="images/a.png" title="Indeks dodał: Kempisty"></a>'
            '<a class="gt" target ="doc" href="https://metryki.genealodzy.pl/id689-sy1910-kt2"><img src="images/s.png"></a>'
            '<a href="fix.php?gid=4654371&bdm=S&w=07mz&rid=1638&lang=eng"><img src="images/fix.png"></a>')
    row = geneteka.parse_row([1910, "2", "Antoni ", "Murawski", "Maciej, Marcjanna", "Marianna ", "Sokolowska", "Mateusz, Marianna", "Zaremby", cell], "S")
    assert row["groom"]["surname"] == "Murawski" and row["bride"]["given"] == "Marianna" and row["parish"] == "Zaremby"
    assert row["scan_url"].endswith("sy1910-kt2") and row["record_gid"] == "4654371" and row["parish_id"] == "1638" and row["indexed_by"] == "Kempisty"
    assert geneteka.search(region="zz", surname="x")["classification"] == "invalid-request"
    assert geneteka.parse_row([1890, "5", "Jan", "Kowal", cell], "B")["cells"][:2] == ["1890", "5"]


def test_bind_to_limits_the_citation_and_plan_lists_every_binding():
    from ancestry_cli import merge_payload as mp
    comparison = {"RecordNodes": {"1:99": {
        "Name": {"Tree": {"AssertionId": "N1", "Given": "Caroline", "Surname": "Lupa"}},
        "Events": [{"Type": "Birth", "Tree": {"AssertionId": "B1", "Date": "1894"}, "Record": {"Date": "1895"}},
                   {"Type": "Residence", "Tree": {"AssertionId": "R1", "Place": "Shamokin"}, "Record": {"Place": "Shamokin"}},
                   {"Type": "Immigration", "Tree": {}, "Record": {"Date": "1910"}}]}}}
    everything = mp.plan(comparison, cite_only=False)
    assert [b["assertion_id"] for b in everything["will_bind"]] == ["N1", "B1", "R1"] and everything["will_create"][0]["type"] == "Immigration"
    chosen = mp.plan(comparison, cite_only=True, bind_to=["R1"])
    assert [b["assertion_id"] for b in chosen["will_bind"]] == ["R1"] and chosen["will_create"] == []
    body = mp._build_upload_body(comparison, hint_id=None, person_gid="1:1030:2", source_gid="3:4", cite_only=True, bind_to=["R1"])
    node = body["Nodes"]["1:99"]
    assert [e["AssertionId"] for e in node["Events"]] == ["R1"] and node["Names"] == []


def test_hint_decision_journal_rows_keep_the_hint_id(tmp_path, monkeypatch):
    from ancestry_cli import journal
    monkeypatch.setenv("ANCESTRY_CLI_JOURNAL", str(tmp_path / "j.jsonl"))
    journal.record("hint-no", 1, 2, {"hint_id": "1016675053917"}, {"hintId": "1016675053917"})
    assert journal._rows()[0]["hint_id"] == "1016675053917"
