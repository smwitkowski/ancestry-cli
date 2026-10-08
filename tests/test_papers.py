import pytest

from ancestry_cli import papers as pp


def test_ids_and_hits():
    assert pp._parse_id("loc:sn87075008/1935-02-14/ed-1/seq-7") == ("loc", "sn87075008", "1935-02-14", 1, 7)
    for bad in ("loc:sn87075008/1935-02-14", "xx:sn1/1935-02-14/ed-1/seq-7", "loc:../1935-02-14/ed-1/seq-7"):
        with pytest.raises(ValueError):
            pp._parse_id(bad)
    assert pp._hit_id("loc", "http://www.loc.gov/resource/sn87075008/1935-02-14/ed-1/?sp=7") == "loc:sn87075008/1935-02-14/ed-1/seq-7"
    assert pp._hit_id("pa", "lccn/sn85054904/2002-10-03/ed-1/seq-24/") == "pa:sn85054904/2002-10-03/ed-1/seq-24"


def test_dates_and_snippets_and_crops():
    assert pp._year_or_date("1935", False) == "1935-01-01" and pp._year_or_date("1935", True) == "1935-12-31"
    assert pp._year_or_date("1935-03", True) == "1935-03-31" and pp._year_or_date(None, True) is None
    with pytest.raises(ValueError):
        pp._year_or_date("35", False)
    assert "Ringgold" in pp._snippet("a" * 400 + " Ringgold " + "b" * 400, ["ringgold"])
    assert pp._crop_box("0.1,0.1,0.5,0.5", 1000, 2000) == (100, 200, 500, 1000)
    with pytest.raises(ValueError):
        pp._crop_box("5000,0,10,10", 1000, 2000)


def test_only_known_hosts_are_fetched():
    with pytest.raises(ValueError):
        pp._get("https://example.com/x")
    with pytest.raises(ValueError):
        pp._get("https://www.loc.gov.evil.example/x")


def test_bad_arguments_never_hit_the_network():
    assert pp.search(q="")["classification"] == "invalid-request"
    assert pp.search(q="x", provider="nys")["classification"] == "invalid-request"


def test_loc_phantom_total_of_one_means_no_hits(monkeypatch):
    monkeypatch.setattr(pp, "_json", lambda url: {"pagination": {"total": 1}, "results": []})
    assert pp._search_loc("x", None, None, None, None, 5, 1) == {"total": 0, "hits": []}
