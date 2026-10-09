"""`geneteka`: Polish parish-index search (Geneteka, genealodzy.pl) over its public JSON endpoint. Read-only.

  geneteka regions
  geneteka search --region 07mz --type S --surname Murawski [--given Antoni] [--from 1908 --to 1912]
                  [--surname2 S --given2 G] [--exact] [--parents] [--parish-id N] [--start 0] [--length 50] [--refresh]

Geneteka's robots.txt asks for a 120 second crawl delay, so every request waits for that gap under a lock
(GENETEKA_CLI_LOCK_FILE shares it) and results are cached for 24 hours (--refresh skips the cache). Requests identify the tool.
A search covers ONE region; it never means "not in Poland". The result keeps the site's own counters (`reported_total`,
`reported_filtered`) apart from the rows actually returned (`returned`): they have been seen to disagree, so a short or empty
result is never proof of absence. `--type S` (marriages) is parsed into named fields; B and D rows keep their raw `cells`.
Every row carries the parish id, the indexer, the archive that holds the books and, when the site offers one, the link to
the scanned register (metryki.genealodzy.pl).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from . import runtime as rt
from .runtime import LaneError, failure

HOST = "https://geneteka.genealodzy.pl"
UA = "ancestry-cli geneteka (personal genealogy research; read-only; 120 s crawl delay; https://github.com/smwitkowski/ancestry-cli)"
PACE = 120.0
CACHE_SECONDS = 24 * 3600
REGIONS = {"01ds": "dolnośląskie", "02kp": "kujawsko-pomorskie", "03lb": "lubelskie", "04ls": "lubuskie", "05ld": "łódzkie",
           "06mp": "małopolskie", "07mz": "mazowieckie", "71wa": "Warszawa", "08op": "opolskie", "09pk": "podkarpackie",
           "10pl": "podlaskie", "11pm": "pomorskie", "12sl": "śląskie", "13sk": "świętokrzyskie", "14wm": "warmińsko-mazurskie",
           "15wp": "wielkopolskie", "16zp": "zachodniopomorskie", "21uk": "Ukraina", "22br": "Białoruś", "23lt": "Litwa"}
TYPES = {"B": "births/baptisms", "S": "marriages", "D": "deaths/burials"}


def lock_file():
    override = os.environ.get("GENETEKA_CLI_LOCK_FILE")
    return Path(override) if override else Path(tempfile.gettempdir()) / f"geneteka-cli-locks-{os.getuid()}" / "http.lock"


def _cache_dir():
    return config.HOME / "geneteka-cache"


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).replace("\r", "").strip()


def _extras(cell):
    """The icon cell: remarks/place, the archive holding the books, who indexed it, the scan link, the record and parish ids."""
    out = {}
    note = re.search(r'src="images/i\.png"\s+title="([^"]*)"', cell)
    if note:
        out["details"] = " ".join(x.strip() for x in html.unescape(note.group(1)).replace("\r", "").split("\n") if x.strip()) or None
    arch = re.search(r'<a href="([^"]+)"[^>]*><img src="images/z\.png"\s+title="([^"]*)"', cell)
    if arch:
        out["archive"] = {"url": arch.group(1), "holds_books": " ".join(x.strip() for x in html.unescape(arch.group(2)).replace("Miejsce przechowywania ksiąg:", "").split("\n") if x.strip())}
    who = re.search(r'src="images/a\.png"\s+title="[^:"]*:\s*([^"]*)"', cell)
    if who:
        out["indexed_by"] = html.unescape(who.group(1)).strip()
    scan = re.search(r'<a class="gt"[^>]*href="([^"]+)"', cell)
    if scan:
        out["scan_url"] = html.unescape(scan.group(1))
    fix = re.search(r"gid=(\d+)&(?:amp;)?bdm=(\w)&(?:amp;)?w=(\w+)&(?:amp;)?rid=(\d+)", cell)
    if fix:
        out["record_gid"], out["parish_id"] = fix.group(1), fix.group(4)
    return out


def parse_row(row, kind):
    cells = [str(c).strip() if not isinstance(c, str) or "<" not in c else c for c in row]
    extras = _extras(cells[-1]) if cells and "<" in str(cells[-1]) else {}
    plain = [c.strip() if isinstance(c, str) else c for c in cells[:-1]] if extras else cells
    if kind == "S" and len(plain) >= 9:
        y, act, gg, gs, gp, bg, bs, bp, parish = plain[:9]
        return {"year": y, "act": act, "groom": {"given": gg, "surname": gs, "parents": gp}, "bride": {"given": bg, "surname": bs, "parents": bp},
                "parish": parish, **extras}
    return {"cells": plain, **extras}


def _fetch(params):
    key = hashlib.sha256(json.dumps(sorted(params.items())).encode()).hexdigest()[:24]
    cache = _cache_dir() / f"{key}.json"
    return key, cache


def _get(params, refresh):
    key, cache = _fetch(params)
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_SECONDS:
        return json.loads(cache.read_text()), True
    url = HOST + "/api/getAct.php?" + urllib.parse.urlencode(params)
    with rt.quiet(), rt.lock("geneteka", path=lock_file(), interval=PACE):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body, status = resp.read(5_000_000), resp.status
        except urllib.error.HTTPError as exc:
            body, status = exc.read(50_000), exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LaneError("geneteka-unavailable") from None
    if status == 429 or status == 403:
        raise LaneError("geneteka-blocked", status=status)
    if not 200 <= status < 300:
        raise LaneError("geneteka-unavailable", status=status)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise LaneError("geneteka-unavailable", status=status) from None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False))
    return data, False


def search(*, region, type_="S", surname=None, given=None, surname2=None, given2=None, frm=None, to=None, exact=False, parents=False,
           parish_id=None, start=0, length=50, refresh=False):
    if region not in REGIONS or type_ not in TYPES or not (surname or given) or not 0 < int(length) <= 50 or int(start) < 0:
        return failure("invalid-request", problems=[{"field": "region" if region not in REGIONS else "type" if type_ not in TYPES else "name", "issue": "invalid"}])
    params = {"op": "gt", "lang": "eng", "bdm": type_, "w": region, "rid": str(parish_id or ""), "search_lastname": surname or "",
              "search_name": given or "", "search_lastname2": surname2 or "", "search_name2": given2 or "", "from_date": str(frm or ""),
              "to_date": str(to or ""), "draw": "1", "start": str(int(start)), "length": str(int(length))}
    if exact:
        params["exac"] = "1"
    if parents:
        params["parents"] = "1"
    if surname2 or given2:
        params["pair"] = "1"
    try:
        data, cached = _get(params, refresh)
    except LaneError as exc:
        return failure(exc.code, **exc.detail)
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")
    rows = [parse_row(r, type_) for r in data.get("data") or []]
    total, filtered = data.get("recordsTotal"), data.get("recordsFiltered")
    return {"ok": True, "classification": "geneteka-search", "dispatch_attempted": not cached, "state": "unchanged", "from_cache": cached,
            "query": {k: v for k, v in {"region": region, "region_name": REGIONS[region], "type": type_, "surname": surname, "given": given,
                                         "surname2": surname2, "given2": given2, "from": frm, "to": to, "exact": exact, "parents": parents,
                                         "parish_id": parish_id, "start": int(start), "length": int(length)}.items() if v not in (None, "", False)},
            "reported_total": total, "reported_filtered": filtered, "returned": len(rows), "rows": rows,
            "caveat": ("One region only. The site's counters and the rows returned are reported separately and have been seen to disagree"
                       + ("; here they do" if isinstance(filtered, int) and filtered != len(rows) and int(start) == 0 else "")
                       + ". An empty or short result is not proof that the act does not exist."),
            "next": {"start": int(start) + len(rows)} if isinstance(filtered, int) and int(start) + len(rows) < filtered and rows else None}


def build_parser():
    p = argparse.ArgumentParser(prog="geneteka", description="Geneteka (Polish parish indexes), read-only, 120 s crawl delay.")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("regions", help="the region codes the search takes")
    s = subs.add_parser("search", help="search one region's index")
    s.add_argument("--region", required=True, help="e.g. 07mz (see `geneteka regions`)")
    s.add_argument("--type", dest="type_", choices=sorted(TYPES), default="S", help="S marriages (default), B births/baptisms, D deaths/burials")
    s.add_argument("--surname")
    s.add_argument("--given")
    s.add_argument("--surname2", help="the spouse's (S) or mother's surname, for a paired search")
    s.add_argument("--given2")
    s.add_argument("--from", dest="frm", type=int)
    s.add_argument("--to", type=int)
    s.add_argument("--exact", action="store_true", help="exact spelling instead of similar names")
    s.add_argument("--parents", action="store_true", help="also match names in the parents columns")
    s.add_argument("--parish-id", type=int)
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--length", type=int, default=50)
    s.add_argument("--refresh", action="store_true", help="skip the 24 hour cache")
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
    if command == "regions":
        result = {"ok": True, "classification": "geneteka-regions", "regions": REGIONS, "types": TYPES}
    else:
        result = search(**args)
    result = rt.annotate(result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
