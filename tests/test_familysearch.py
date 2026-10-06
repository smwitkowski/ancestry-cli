import json

import pytest

from ancestry_cli import familysearch as fs
from ancestry_cli import runtime as rt


def test_only_listed_requests_are_allowed():
    ok = [{"path": "/ark:/61903/1:1:NLLS-XFK?useSLS=true&useRolesOverride=false"}, {"path": "/platform/tree/persons/9Z31-JPT/sources"},
          {"path": "/service/records/storage/deepzoomcloud/dz/v1/TH-909-62953-77901-7/image_files/12/3_4.jpg"},
          {"path": "/search/filmdatainfo/film-data", "method": "POST"}]
    for c in ok:
        fs._check(c)
    for bad in ("/platform/tree/persons/9Z31-JPT/delete", "https://evil.example/x", "/service/ident/session/sessions/CURRENT",
                "/ark:/61903/1:1:A?useSLS=true&useRolesOverride=false&x=1"):
        with pytest.raises(ValueError):
            fs._check({"path": bad})
    with pytest.raises(ValueError):
        fs._check({"path": "/platform/tree/persons/9Z31-JPT", "method": "POST"})


@pytest.mark.parametrize("row,code", [({"status": 401}, "familysearch-sign-in-required"), ({"status": 302, "redirected": True}, "familysearch-sign-in-required"),
                                      ({"status": 200, "text": "<title>Just a moment...</title>"}, "familysearch-check-required"),
                                      ({"status": 429}, "familysearch-rate-limited"), ({"status": 404}, "familysearch-not-found"),
                                      ({"status": 500}, "familysearch-unavailable"), ({"status": 0}, "familysearch-unavailable")])
def test_classify(row, code):
    with pytest.raises(rt.LaneError) as e:
        fs._classify(row)
    assert e.value.code == code


def test_tile_plan_fits_the_budget_and_crop_math():
    level, x0, y0, x1, y1, scale = fs._plan(9238, 6452, None, 24)
    assert (level, scale) == (11, 8)            # 1155 x 807 px = 5 x 4 tiles
    full, *_ = fs._plan(9238, 6452, (0, 0, 9238, 6452), 10_000)
    assert full == 14
    assert fs._crop_box("0.1,0.2,0.3,0.2", 1000, 500) == (100, 100, 300, 100)
    with pytest.raises(ValueError):
        fs._crop_box("900,0,500,10", 1000, 500)


def test_person_projection_reads_gedcomx_parts():
    p = {"principal": True, "gender": {"type": "http://gedcomx.org/Female"},
         "names": [{"nameForms": [{"fullText": "A B"}]}], "identifiers": {"http://gedcomx.org/Persistent": ["https://x/ark:/61903/1:1:NLLS-XFK"]},
         "facts": [{"type": "http://gedcomx.org/Birth", "date": {"original": "1846"}, "place": {"original": "X"}}]}
    out = fs._person(p)
    assert out["ark"] == "1:1:NLLS-XFK" and out["gender"] == "Female" and out["facts"] == [{"type": "Birth", "date": "1846", "place": "X"}]


def test_bad_identifiers_are_usage_errors_and_nothing_is_sent():
    assert fs.record("not-an-ark")["classification"] == "invalid-request"
    assert fs.person("lower")["classification"] == "invalid-request"
    assert fs.film("12x")["classification"] == "invalid-request"


def test_permission_lists_grant_when_anyone_is_in_them():
    assert fs._viewable("ThemisPrmAncestry:ThemisPrmAnyone:ThemisPrmFindMyPast") and fs._viewable("ThemisPrmAnyone")
    assert not fs._viewable("ThemisPrmAncestry:ThemisPrmFindMyPast")


def test_image_arks_come_from_artifacts_and_extra_data():
    g = {"sourceDescriptions": [{"resourceType": "http://gedcomx.org/DigitalArtifact", "about": "https://www.familysearch.org/ark:/61903/3:1:AAA-BBB-CCC"},
                                {"resourceType": "http://gedcomx.org/Person", "about": "https://x/ark:/61903/1:1:ZZ"}],
         "fields": [{"values": [{"labelId": "EXT_DATA", "text": '{"IMAGE_ARK":"https://familysearch.org/ark:/61903/3:1:DDD-EEE","X":"1"}'}]}]}
    assert fs._image_arks(g) == ["3:1:AAA-BBB-CCC", "3:1:DDD-EEE"]
    assert fs._image_arks({}) == []


def test_catalog_inputs_and_title_years():
    assert fs._catalog_years("Kirchenbuch, 1702-1960") == (1702, 1960) and fs._catalog_years("Kirchenbuch") is None
    assert fs.catalog(place="x<y")["classification"] == "invalid-request"
    assert fs.catalog(place="Rockenhausen", years="abc")["classification"] == "invalid-request"
    fs._check({"path": "/service/search/catalog/item/olib:1485663"})
    fs._check({"path": "/service/search/catalog/v3/search?count=20&q.place=Rockenhausen&q.subjectId=133492089"})


def test_dates_as_written_in_registers():
    assert fs._date_key("10. September 1843") == (1843, 9, 10)
    assert fs._date_key("1 Nov 1846") == (1846, 11, 1)
    assert fs._date_key("März 1850") == (1850, 3, 0) and fs._date_key("1846") == (1846, 0, 0) and fs._date_key("undated") is None
    assert fs._target("1846-11-01", None) == (fs._flat((1846, 11, 1)),) * 2
    lo, hi = fs._target(None, "1840-1850")
    assert lo == fs._flat((1840, 0, 0)) and hi == fs._flat((1850, 12, 31))


def test_film_sections_come_from_the_contents_text():
    data = {"dgsNum": "5", "catalogs": [{"data": {"film_note": [{"digital_film_no": "5", "text": "Heiraten 1798-1839 -- Familien-Verzeichnis -- Taufen 1839-1868"}]}}]}
    assert fs._sections(data) == [{"label": "Heiraten 1798-1839", "years": [1798, 1839]}, {"label": "Familien-Verzeichnis", "years": None},
                                  {"label": "Taufen 1839-1868", "years": [1839, 1868]}]


def test_locate_bisects_to_the_bracket(monkeypatch):
    # 400 images; baptism dates rise one month per ten images from 1840 (March 1842 is images 260-269)
    def fake_probe(arks, n):
        y, m = 1840 + (n // 10) // 12, 1 + (n // 10) % 12
        return {"image": n, "records": 1, "names": [], "events": [{"type": "Baptism", "date": f"5 {m} {y}", "key": (y, m, 5), "name": "X"}]}
    monkeypatch.setattr(fs, "_probe", fake_probe)
    monkeypatch.setattr(fs, "_film_data", lambda dgs: ({"dgsNum": str(dgs)}, [f"3:1:A-{i}" for i in range(400)]))
    monkeypatch.setattr(fs, "_guard", lambda fn: fn())
    out = fs.locate(film_dgs="5", type_="baptisms", date="1842-03-05", probes=16)
    c = out["candidates"][0]
    assert c["from"] <= 265 <= c["to"] and c["to"] - c["from"] <= 40 and out["probes"] <= 16
