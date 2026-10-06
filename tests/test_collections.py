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
