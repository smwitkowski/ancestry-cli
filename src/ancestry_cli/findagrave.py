"""`findagrave`: Find a Grave memorials over plain HTTP (read-only, public pages).

Find a Grave's robots.txt disallows `/memorial/search` and cemetery memorial searches for automated clients, so this tool does NOT search.
Get a memorial id from your browser, from a source citation or from a family link on another memorial, then:

  findagrave doctor
  findagrave memorial ID [--photos]     (dates, places, cemetery and plot, bio, inscription, family links with memorial ids)
  findagrave photo ID --out FILE [--n 1]   (save the memorial's Nth photo)

Requests identify the tool, are 2 s apart under a lock (FINDAGRAVE_CLI_LOCK_FILE shares it), and stop on a bot check or 429.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import runtime as rt
from .runtime import LaneError, failure

BASE = "https://www.findagrave.com"
UA = "ancestry-cli findagrave (personal genealogy research; read-only; https://github.com/smwitkowski/ancestry-cli)"
PACE = 2.0
_MEM = re.compile(r"[0-9]{1,12}\Z")


def lock_file():
    override = os.environ.get("FINDAGRAVE_CLI_LOCK_FILE")
    return Path(override) if override else Path(tempfile.gettempdir()) / f"findagrave-cli-locks-{os.getuid()}" / "http.lock"


def _get(url, *, binary=False):
    path = url[len(BASE):] if url.startswith(BASE) else ""
    if not (re.fullmatch(r"/memorial/[0-9]{1,12}(?:/[A-Za-z0-9_%-]{1,120}(?:/photo)?)?", path) or url == BASE + "/robots.txt"
            or re.fullmatch(r"https://images\.findagrave\.com/photos/[A-Za-z0-9_/.-]{1,200}", url)):
        raise ValueError("url-not-allowed")          # never /memorial/search or /edit: robots.txt disallows them
    with rt.quiet(), rt.lock("findagrave", path=lock_file(), interval=PACE):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*" if binary else "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body, status, final = resp.read(30_000_000), resp.status, resp.geturl()
        except urllib.error.HTTPError as exc:
            body, status, final = exc.read(200_000), exc.code, url
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LaneError("findagrave-unavailable") from None
    if status in (403, 503) or b"Just a moment" in body[:3000]:
        raise LaneError("findagrave-check-required", status=status, openssl=__import__("ssl").OPENSSL_VERSION.split()[1])
    if status == 429:
        raise LaneError("findagrave-rate-limited", status=429)
    if status == 404:
        raise LaneError("findagrave-not-found", status=404)
    if not 200 <= status < 300:
        raise LaneError("findagrave-unavailable", status=status)
    if url.startswith(BASE + "/memorial/") and not binary and not final.startswith(BASE + "/memorial/"):
        raise LaneError("findagrave-not-found", status=404)
    return body if binary else body.decode("utf-8", "replace")


def _guard(fn):
    try:
        return fn()
    except LaneError as exc:
        return failure(exc.code, **exc.detail)
    except ValueError:
        return failure("invalid-request")
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")


def _text(fragment):
    fragment = re.sub(r"<br\s*/?>", "\n", fragment or "")
    return html.unescape(re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", "", fragment))).strip()


def _grab(h, pattern, group=1):
    m = re.search(pattern, h, re.DOTALL)
    return _text(m.group(group)) if m else None


_RELATION = {"parent": "parents", "spouse": "spouses", "sibling": "siblings", "halfsibling": "half_siblings", "children": "children", "child": "children"}


def _name(h):
    m = re.search(r'<h1 id="bio-name"[^>]*>(.*?)</h1>', h, re.DOTALL)
    return _text(re.sub(r"<b\b.*?</b>", "", m.group(1), flags=re.DOTALL)) or None if m else None   # drop the veteran badge


def parse_memorial(h, mid):
    """Everything comes from the page's own markup; missing parts are None or empty."""
    cemetery = None
    m = re.search(r'<a href="(/cemetery/(\d+)/[^"]*)"[^>]*>\s*<span id="cemeteryNameLabel"[^>]*>(.*?)</span>', h, re.DOTALL)
    if m:
        cemetery = {"id": m.group(2), "name": _text(m.group(3)), "url": BASE + m.group(1),
                    "city": _grab(h, r'id="cemeteryCityName"[^>]*>(.*?)</span>'), "county": _grab(h, r'id="cemeteryCountyName"[^>]*>(.*?)</span>'),
                    "state": _grab(h, r'id="cemeteryStateName"[^>]*>(.*?)</span>')}
    gps = re.search(r"GPS-Latitude: (-?[\d.]+), Longitude: (-?[\d.]+)", h)
    family = {}
    for label_id, label, rest in re.findall(r'<b id="(\w+?)Label" class="label-relation">(.*?)</b>\s*<ul class="member-family"[^>]*>(.*?)</ul>', h, re.DOTALL):
        key = _RELATION.get(re.sub(r"[^a-z]", "", _text(label).lower().rstrip("s")) or label_id.lower(), None) or _RELATION.get(label_id.lower())
        if not key:
            key = re.sub(r"[^a-z]+", "_", _text(label).lower()).strip("_")
        people = []
        for li in re.findall(r"<li[^>]*>(.*?)</li>", rest, re.DOTALL):
            link = re.search(r'href="/memorial/(\d+)/([^"]*)"', li)
            if not link:
                continue
            years = re.findall(r'itemprop="(?:birthDate|deathDate)"[^>]*>(.*?)</span>', li, re.DOTALL)
            people.append({"memorial_id": link.group(1), "name": _grab(li, r'<h3[^>]*>(.*?)</h3>'),
                           "born": _text(years[0]) if len(years) > 0 else None, "died": _text(years[1]) if len(years) > 1 else None,
                           "url": f"{BASE}/memorial/{link.group(1)}/{link.group(2)}"})
        family.setdefault(key, []).extend(people)
    bio = _grab(h, r'id="fullBio"[^>]*>(.*?)</div>') or _grab(h, r'id="partBio"[^>]*>(.*?)</div>')
    photos = _grab(h, r'id="photo-count"[^>]*>(.*?)</') or _grab(h, r'(\d+)\s*Photos?\b', 1)
    return {"memorial_id": _grab(h, r'id="memNumberLabel"[^>]*>(.*?)</span>') or str(mid), "name": _name(h),
            "birth": {"date": _grab(h, r'id="birthDateLabel"[^>]*>(.*?)</'), "place": _grab(h, r'id="birthLocationLabel"[^>]*>(.*?)</div>')},
            "death": {"date": _grab(h, r'id="deathDateLabel"[^>]*>(.*?)</'), "place": _grab(h, r'id="deathLocationLabel"[^>]*>(.*?)</div>')},
            "cemetery": cemetery, "plot": _grab(h, r"Plot</span>\s*</dt>\s*<dd[^>]*>(.*?)</dd>"),
            "gps": {"lat": float(gps.group(1)), "lon": float(gps.group(2))} if gps else None,
            "inscription": _grab(h, r'id="inscriptionValue"[^>]*>(.*?)</'), "bio": bio, "family": family,
            "photo_count": int(photos) if photos and photos.isdigit() else None}


def parse_photos(h):
    out = []
    for block in re.findall(r'<div class="viewer-item [^"]*"[^>]*data-photo-id="(\d+)".*?(?=<div class="viewer-item |\Z)', h, re.DOTALL):
        pass
    for m in re.finditer(r'<div class="viewer-item [^"]*"[^>]*data-photo-id="(\d+)"(.*?)(?=<div class="viewer-item |\Z)', h, re.DOTALL):
        src = re.search(r'data-src="(https://images\.findagrave\.com/photos/[^"?]+)', m.group(2))
        if src:
            out.append({"n": len(out) + 1, "photo_id": m.group(1), "url": src.group(1)})
    return out


def _slugless(mid):
    if not _MEM.match(str(mid)):
        raise ValueError("id")
    return f"{BASE}/memorial/{mid}"


def memorial(mid, *, photos=False):
    try:
        base = _slugless(mid)
    except ValueError:
        return failure("invalid-request", problems=[{"field": "id", "issue": "invalid", "expected": "digits"}])

    def run():
        h = _get(base)
        out = {"ok": True, "classification": "findagrave-memorial", "dispatch_attempted": True, "state": "unchanged",
               **parse_memorial(h, mid), "url": base}
        if not out["name"]:
            raise LaneError("findagrave-not-found", status=404)
        if photos:
            slug = re.search(rf'href="(/memorial/{mid}/[^"/]+)/photo', h)
            out["photos"] = parse_photos(_get(BASE + slug.group(1) + "/photo")) if slug else []
            out["photo_count"] = len(out["photos"])
        return out
    return _guard(run)


def photo(mid, *, out, n=1):
    try:
        base = _slugless(mid)
    except ValueError:
        return failure("invalid-request", problems=[{"field": "id", "issue": "invalid", "expected": "digits"}])
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir():
        return failure("invalid-request", problems=[{"field": "out", "issue": "invalid", "expected": "a new file in an existing folder"}])

    def run():
        h = _get(base)
        slug = re.search(rf'href="(/memorial/{mid}/[^"/]+)/photo', h)
        items = parse_photos(_get(BASE + slug.group(1) + "/photo")) if slug else []
        if not 1 <= int(n) <= len(items):
            raise LaneError("findagrave-not-found", status=404, photos=len(items))
        data = _get(items[int(n) - 1]["url"], binary=True)
        dest.write_bytes(data)
        dest.chmod(0o600)
        return {"ok": True, "classification": "findagrave-photo", "dispatch_attempted": True, "state": "unchanged", "memorial_id": str(mid),
                "photo_id": items[int(n) - 1]["photo_id"], "n": int(n), "of": len(items), "file": str(dest), "bytes": len(data)}
    return _guard(run)


def doctor():
    def run():
        _get(BASE + "/robots.txt")
        return {"ok": True, "classification": "doctor", "lock_file": str(lock_file()), "site": "ok",
                "search": "not supported: robots.txt disallows /memorial/search for automated clients"}
    return _guard(run)


def build_parser():
    p = argparse.ArgumentParser(prog="findagrave", description="Find a Grave memorials, read-only (no search: robots.txt disallows it).")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="check the site")
    s = subs.add_parser("search", help="not supported (robots.txt)")
    s.add_argument("terms", nargs="*")
    m = subs.add_parser("memorial", help="one memorial: dates, places, cemetery, bio, family links")
    m.add_argument("id")
    m.add_argument("--photos", action="store_true", help="also list the memorial's photos")
    ph = subs.add_parser("photo", help="save one of a memorial's photos")
    ph.add_argument("id")
    ph.add_argument("--out", required=True)
    ph.add_argument("--n", type=int, default=1, help="which photo, starting at 1")
    return p


def main(argv=None):
    try:
        args = vars(build_parser().parse_args(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        print(json.dumps(rt.annotate(failure("usage-error"))))
        return 2
    command = args.pop("command")
    if command == "doctor":
        result = doctor()
    elif command == "search":
        result = failure("findagrave-search-not-allowed")
    elif command == "memorial":
        result = memorial(args["id"], photos=args["photos"])
    else:
        result = photo(args["id"], out=args["out"], n=args["n"])
    result = rt.annotate(result)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
