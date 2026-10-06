"""Newspapers.com page facts (read-only), through the signed-in Newspapers.com tab in the same Chrome.

`ancestry newspapers page --page ID` reads https://www.newspapers.com/newspage/ID/ from inside that tab and reports the paper, the issue,
whether the signed-in account can read the page, and the "Headlines & Names Mentioned" index, which is public even for pages the
account cannot open. The tab is the user's: sign in there by hand (Ancestry sign-in works). This tool never navigates it or types
credentials. Cloudflare may require the user to pass its check in that tab once.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from . import runtime as rt
from .runtime import failure

ORIGIN = "https://www.newspapers.com"


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in = None
        self._link = None
        self.text = []
        self.links = []                  # (text, href)
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "title":
            self._in = "title"
        if tag == "a":
            self._link = [dict(attrs).get("href", ""), []]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        if tag == "title":
            self._in = None
        if tag == "a" and self._link:
            self.links.append(("".join(self._link[1]).strip(), self._link[0]))
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in == "title":
            self.title += data
        else:
            self.text.append(data)
            if self._link:
                self._link[1].append(data)


def parse(html, page_id):
    """Everything is read from the page's own markup: no values are invented."""
    p = _Page()
    p.feed(html)
    text = re.sub(r"\s+", " ", " ".join(p.text))
    articles = {}
    for label, href in p.links:
        if f"/image/{page_id}/" not in href or "article=" not in href or not label:
            continue
        query = parse_qs(urlsplit(href).query)
        art = (query.get("article") or [""])[0]
        entry = articles.setdefault(art, {"article": art, "headline": None, "names": []})
        if query.get("terms"):
            if label not in entry["names"]:
                entry["names"].append(label)
        elif entry["headline"] is None:
            entry["headline"] = label
    title = re.sub(r"\s*-\s*Newspapers\.com.*$", "", p.title.strip())
    return {"title": title,
            "publisher_extra": "A Publisher Extra" in text,
            "page_gated": "Get access to this page" in text,
            "logged_in": '"isLoggedIn":true' in html,
            "subscriber": ('"isSubscriber":true' in html) if '"isSubscriber"' in html else None,
            "more_articles": int((re.search(r"Show (\d+) more articles", text) or [0, 0])[1]),
            "articles": list(articles.values())}


def page(*, page_id, bridge=None):
    if not page_id or not str(page_id).isdigit():
        return failure("missing-arguments")
    bridge = bridge or rt.load_bridge()
    try:
        with rt.quiet(), rt.lock("newspapers"):
            lease = rt.Lease(bridge, origin=ORIGIN).check()
            status, html = bridge.fetch_newspage(lease.base, lease.target_id, page_id)
    except rt.LaneError as exc:
        code = {"browser-lane-site-required": "newspapers-tab-required", "browser-lane-ambiguous": "newspapers-tab-ambiguous"}.get(exc.code, exc.code)
        return failure(code, **exc.detail)
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "native-operation-failed")
    if status in (403, 429) or "Just a moment" in html[:3000]:
        return failure("newspapers-check-required", status=status)
    if status != 200:
        return failure("newspapers-unavailable", status=status)
    info = parse(html, page_id)
    if not info["logged_in"]:
        return failure("newspapers-sign-in-required")
    return {"ok": True, "classification": "newspapers-page", "dispatch_attempted": True, "state": "unchanged", "page_id": str(page_id),
            **info, "can_open": not info["page_gated"] and bool(info["subscriber"]),
            "note": "page_gated is the archive page's own paywall notice. The image viewer can still open some older or free pages "
                    "for a non-subscriber; the names list is available either way."}
