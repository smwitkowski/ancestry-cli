import pytest

from ancestry_cli import findagrave as fg

PAGE = """<html><h1 id="bio-name" class="bio-name" itemprop="name"><span class="prefix">CPL</span> Paul Ringgold <b class="icon-vet badge"><span>Veteran</span></b></h1>
<span id="memNumberLabel" class="hidden">42</span>
<time id="birthDateLabel" itemprop="birthDate">6 Jan 1828</time><div id="birthLocationLabel" class="place">Queen Anne&#39;s, MD</div>
<span id="deathDateLabel" itemprop="deathDate">13 Feb 1904 (aged 76)</span><div id="deathLocationLabel">Baltimore, MD</div>
<a href="/cemetery/81661/stevensville-cemetery" class="x"><span id="cemeteryNameLabel" itemprop="name">Stevensville Cemetery</span></a>
<span id="cemeteryCityName">Stevensville</span>, <span id="cemeteryStateName">Maryland</span>
<span id="plotLabel">Plot</span></dt><dd class="x">Section 3</dd>
<a title="GPS-Latitude: 38.9, Longitude: -76.3">map</a>
<div id="fullBio">Married twice.<br />Census 1850</div>
<b id="parentLabel" class="label-relation">Parents</b><ul class="member-family" aria-labelledby="parentLabel">
<li><a href="/memorial/7/john-ringgold" itemprop="url"><h3 itemprop="name"> John Ringgold </h3><p><span itemprop="birthDate">1800</span>&ndash;<span itemprop="deathDate">1870</span></p></a></li></ul>
<b id="spouseLabel" class="label-relation">Spouses</b><ul class="member-family"><li><a href="/memorial/8/mary-ringgold"><h3>Mary</h3></a></li></ul>
<b id="childrenLabel" class="label-relation">Children</b><ul class="member-family"></ul>
"""


def test_memorial_parse():
    m = fg.parse_memorial(PAGE, 42)
    assert m["name"] == "CPL Paul Ringgold" and m["memorial_id"] == "42"
    assert m["birth"] == {"date": "6 Jan 1828", "place": "Queen Anne's, MD"} and m["death"]["place"] == "Baltimore, MD"
    assert m["cemetery"]["id"] == "81661" and m["cemetery"]["state"] == "Maryland" and m["plot"] == "Section 3"
    assert m["gps"] == {"lat": 38.9, "lon": -76.3} and m["bio"].startswith("Married twice.\nCensus")
    assert m["family"]["parents"] == [{"memorial_id": "7", "name": "John Ringgold", "born": "1800", "died": "1870",
                                       "url": "https://www.findagrave.com/memorial/7/john-ringgold"}]
    assert m["family"]["spouses"][0]["memorial_id"] == "8"


def test_photos_parse():
    h = '<div class="viewer-item x" data-photo-id="11"><img data-src="https://images.findagrave.com/photos/2025/1/a.jpeg"></div><div class="viewer-item y" data-photo-id="12"><img data-src="https://images.findagrave.com/photos/2010/2/b.jpg?size=x"></div>'
    assert [p["url"] for p in fg.parse_photos(h)] == ["https://images.findagrave.com/photos/2025/1/a.jpeg", "https://images.findagrave.com/photos/2010/2/b.jpg"]


def test_only_memorial_pages_and_photos_are_fetched():
    for bad in ("https://www.findagrave.com/memorial/search?firstname=a", "https://example.com/", "https://www.findagrave.com/cemetery/1/x"):
        with pytest.raises(ValueError):
            fg._get(bad)
    assert fg.memorial("abc")["classification"] == "invalid-request"
