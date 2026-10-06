#!/usr/bin/env python3
"""Read-only Ancestry transport backed by a user-controlled Chrome session.

The bridge talks to Chrome over its DevTools Protocol endpoint and evaluates a
same-origin ``fetch`` in an existing Ancestry page. Chrome supplies the page's
own session state; this module never reads or exports cookies, storage, or
authorization headers.

It is deliberately allowlisted against ``scripts/api_endpoints.py``. Side
effecting routes and requests carrying token-like query parameters are
rejected before Chrome is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import websockets

from .api_endpoints import ENDPOINTS

BASE = "https://www.ancestry.com"
DEFAULT_CDP_URL = "http://127.0.0.1:9224"
SENSITIVE_NAMES = {
    "authorization",
    "cookie",
    "password",
    "securitytoken",
    "token",
    "access_token",
}
SENSITIVE_NAME_RE = re.compile(r"(?:^|[_-])(token|cookie|password|authorization)(?:$|[_-])", re.IGNORECASE)


class BridgeError(RuntimeError):
    """A transport, browser, allowlist, or response error."""


BrowserExecutor = Callable[[str, str, float, bool], Any]


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return lowered in SENSITIVE_NAMES or bool(SENSITIVE_NAME_RE.search(lowered))


def _endpoint_template(method: str, path: str) -> dict[str, Any]:
    """Return the documented read endpoint matching a concrete or template path."""

    method = method.upper()
    for endpoint in ENDPOINTS:
        if endpoint.get("side_effect") or endpoint.get("method") != method:
            continue
        template = endpoint["path"]
        if path == template:
            return endpoint
        pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$"
        if re.match(pattern, path):
            return endpoint
    raise BridgeError(f"endpoint is not in the read-only inventory: {method} {path}")


def validate_request(method: str, path: str, query: dict[str, Any] | None, headers: dict[str, str] | None) -> dict[str, Any]:
    """Validate a request before any browser interaction occurs."""

    if not path.startswith("/") or path.startswith("//"):
        raise BridgeError("path must be an absolute Ancestry path beginning with '/'")
    parsed = urllib.parse.urlsplit(path)
    if parsed.query or parsed.fragment:
        raise BridgeError("put query parameters in query=; embedded query strings are rejected")
    endpoint = _endpoint_template(method, parsed.path)

    for key in (query or {}):
        if _is_sensitive_name(str(key)):
            raise BridgeError(f"sensitive query parameter rejected: {key}")
    for key in (headers or {}):
        if _is_sensitive_name(str(key)):
            raise BridgeError(f"sensitive header rejected: {key}")

    if endpoint.get("method") != "GET":
        # The inventory contains one read-only POST (media exploration). Keep
        # the browser transport narrower until its body contract is needed.
        if endpoint["path"] != "/api/media-explore/media":
            raise BridgeError(f"browser bridge only permits read-only GETs for now: {method} {path}")
    return endpoint


# Group(s) whose side-effecting endpoints may be executed through the
# browser transport via ChromeBrowserSession.post / writes_ok=True GETs.
# Auth must NEVER be called; search_write is UI-bookkeeping (skip).
_WRITE_GROUPS = ("writes", "merge")


def validate_write_request(
    method: str,
    path: str,
    query: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> dict:
    """Validate a side-effecting request before any browser interaction.

    Only endpoints in the `writes`/`merge` inventory groups with
    ``side_effect=True`` may pass. Same safety checks as validate_request:
    absolute path, no embedded query, no sensitive query/header names.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise BridgeError("path must be an absolute Ancestry path beginning with '/'")
    parsed = urllib.parse.urlsplit(path)
    if parsed.query or parsed.fragment:
        raise BridgeError("put query parameters in query=; embedded query strings are rejected")
    endpoint = None
    method = method.upper()
    for candidate in ENDPOINTS:
        if not candidate.get("side_effect") or candidate.get("method") != method:
            continue
        template = candidate["path"]
        if parsed.path != template and not re.match(
            "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$", parsed.path
        ):
            continue
        endpoint = candidate
        break
    if not endpoint:
        raise BridgeError(f"not a tooled side-effecting endpoint: {method} {parsed.path}")
    if endpoint.get("group") not in _WRITE_GROUPS:
        raise BridgeError(
            f"write endpoint group {endpoint.get('group')!r} is not in the tooled write "
            f"allowlist {list(_WRITE_GROUPS)}"
        )
    for key in (query or {}):
        if _is_sensitive_name(str(key)):
            raise BridgeError(f"sensitive query parameter rejected: {key}")
    for key in (headers or {}):
        if _is_sensitive_name(str(key)):
            raise BridgeError(f"sensitive header rejected: {key}")
    return endpoint


def _cdp_json(base_url: str, suffix: str) -> Any:
    _validate_cdp_url(base_url)
    url = base_url.rstrip("/") + suffix
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise BridgeError(f"Chrome CDP is unavailable at {base_url}: {exc}") from exc


def _validate_cdp_url(base_url: str) -> None:
    """Limit CDP to an unauthenticated loopback HTTP endpoint."""

    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeError("CDP URL must be an unauthenticated loopback http:// endpoint")


def diagnose_cdp(base_url: str = DEFAULT_CDP_URL) -> dict[str, Any]:
    """Return a non-sensitive capability diagnosis for a local CDP endpoint."""

    try:
        _validate_cdp_url(base_url)
    except BridgeError:
        return {
            "ok": False,
            "capability": "invalid-cdp-url",
            "detail": "CDP must use an unauthenticated loopback http:// endpoint.",
        }
    try:
        version = _cdp_json(base_url, "/json/version")
        targets = _cdp_json(base_url, "/json/list")
    except BridgeError:
        return {
            "ok": False,
            "capability": "cdp-unavailable",
            "detail": "No loopback Chrome DevTools endpoint is reachable.",
            "next_step": (
                "Use an explicitly remote-debug-enabled Chrome instance, or inject an "
                "opaque BrowserExecutor from the managed browser adapter."
            ),
        }

    ancestry_pages = [
        target for target in targets
        if target.get("type") == "page"
        and ".ancestry." in urllib.parse.urlsplit(target.get("url", "")).netloc
    ]
    browser_ws = bool(version.get("webSocketDebuggerUrl"))
    if not browser_ws:
        return {
            "ok": False,
            "capability": "cdp-incomplete",
            "detail": "Chrome responded, but did not expose a browser WebSocket.",
            "ancestry_page_count": len(ancestry_pages),
        }
    if not ancestry_pages:
        return {
            "ok": False,
            "capability": "ancestry-tab-unavailable",
            "detail": "CDP is reachable, but no Ancestry page is exposed.",
            "ancestry_page_count": 0,
        }
    return {
        "ok": True,
        "capability": "chrome-cdp",
        "ancestry_page_count": len(ancestry_pages),
        "target_selection_required": len(ancestry_pages) > 1,
    }


def _target_for(base_url: str, target_id: str | None) -> dict[str, Any]:
    targets = _cdp_json(base_url, "/json/list")
    pages = [
        target for target in targets
        if target.get("type") == "page" and ".ancestry." in urllib.parse.urlsplit(target.get("url", "")).netloc
    ]
    if target_id:
        pages = [target for target in pages if target.get("id") == target_id]
    if len(pages) != 1:
        if not pages:
            raise BridgeError("no Ancestry page is exposed by Chrome CDP")
        raise BridgeError(f"multiple Ancestry pages are exposed; pass --target-id (count={len(pages)})")
    return pages[0]


@dataclass
class BrowserResponse:
    """Small requests.Response-compatible read result used by ancestry_tools."""

    status_code: int
    url: str
    headers: dict[str, str]
    text: str

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise BridgeError(f"Ancestry browser request returned HTTP {self.status_code}: {self.url}")


class ChromeBrowserSession:
    """Synchronous requests-like session that delegates reads to Chrome."""

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL, target_id: str | None = None, timeout: float = 30.0):
        self.cdp_url = cdp_url
        self.target_id = target_id
        self.timeout = timeout

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        writes_ok: bool = False,
        **_: Any,
    ) -> BrowserResponse:
        """GET through the authenticated tab. By default refuses endpoints the
        inventory flags as side-effecting; pass writes_ok=True only from the
        tooled write helpers (fact_delete / weblink_remove)."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        if writes_ok:
            # Tooled side-effecting GETs (fact_delete / weblink_remove) go
            # through the write allowlist, not the read-only template matcher.
            endpoint = validate_write_request("GET", parsed.path, params, headers)
        else:
            endpoint = validate_request("GET", parsed.path, params, headers)
            if endpoint.get("side_effect"):
                raise BridgeError(
                    f"refusing side-effecting GET {parsed.path}; use the tooled write path "
                    f"(writes_ok=True) only from approved write helpers"
                )
        return self.request(
            "GET", url, params=params, timeout=timeout,
            headers=headers, allow_redirects=allow_redirects,
            _validated_endpoint=endpoint,
        )

    def post(self, url: str, *, params: dict[str, Any] | None = None, json: Any = None,
             data: str | bytes | None = None, timeout: float | None = None,
             headers: dict[str, str] | None = None, **_: Any) -> BrowserResponse:
        """Side-effecting POST executed as a same-origin fetch inside the
        authenticated Ancestry tab. Cookies never leave the browser; the
        endpoint must be in the tooled write allowlist (writes/merge groups)."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        endpoint = validate_write_request("POST", parsed.path, params, headers)
        embedded = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        combined: dict[str, Any] = dict(embedded)
        for key, value in (params or {}).items():
            combined[key] = value
        query = urllib.parse.urlencode(combined, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        import json as _json
        body = _json.dumps(json) if json is not None else (
            data.decode("utf-8") if isinstance(data, bytes) else (data or ""))
        return _run(
            self.cdp_url,
            self.target_id,
            "POST",
            request_url,
            body=body or None,
            headers=headers or None,
            timeout=timeout or self.timeout,
            allow_redirects=False,
        )

    def post_read(self, url: str, *, json: Any, timeout: float | None = None) -> BrowserResponse:
        """The record match explainer: a POST that only reads (it scores one record against a search). Nothing else may use it."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com" or not re.fullmatch(
                r"/api/search-results/record/[0-9]{1,9}/[0-9]{1,15}/", parsed.path):
            raise BridgeError("post_read only permits the record match explainer")
        import json as _json
        return _run(self.cdp_url, self.target_id, "POST", url, body=_json.dumps(json),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=timeout or self.timeout, allow_redirects=False)

    def put_binary(self, url: str, *, data: bytes, content_type: str, params: dict[str, Any] | None = None,
                   timeout: float | None = None, **_: Any) -> BrowserResponse:
        """Side-effecting binary PUT (media upload stream) in the authenticated tab; allowlisted endpoints only."""
        import base64
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= 25 * 1024 * 1024:
            raise BridgeError("binary body must be 1 byte to 25 MB")
        # The per-upload `securityToken` (issued by the media_stoken endpoint, valid for this one media id) is the only
        # token-named parameter admitted anywhere in the bridge; it is excluded from the sensitive-name check, never logged.
        validate_write_request("PUT", parsed.path, {k: v for k, v in (params or {}).items() if k != "securityToken"}, None)
        query = urllib.parse.urlencode({**urllib.parse.parse_qs(parsed.query, keep_blank_values=True), **(params or {})}, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        return _run(self.cdp_url, self.target_id, "PUT", request_url, body_b64=base64.b64encode(bytes(data)).decode("ascii"),
                    headers={"Content-Type": content_type}, timeout=timeout or self.timeout, allow_redirects=False)

    def delete(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None,
               headers: dict[str, str] | None = None, **_: Any) -> BrowserResponse:
        """Side-effecting DELETE in the authenticated tab; the endpoint must be in the write allowlist."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        validate_write_request("DELETE", parsed.path, params, headers)
        query = urllib.parse.urlencode({**urllib.parse.parse_qs(parsed.query, keep_blank_values=True), **(params or {})}, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        return _run(self.cdp_url, self.target_id, "DELETE", request_url, headers=headers or None,
                    timeout=timeout or self.timeout, allow_redirects=False)

    def patch(self, url: str, *, params: dict[str, Any] | None = None, data: str | bytes | None = None,
              timeout: float | None = None, headers: dict[str, str] | None = None, **_: Any) -> BrowserResponse:
        """Side-effecting PATCH in the authenticated tab; the endpoint must be in the write allowlist."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        validate_write_request("PATCH", parsed.path, params, headers)
        query = urllib.parse.urlencode({**urllib.parse.parse_qs(parsed.query, keep_blank_values=True), **(params or {})}, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
        return _run(self.cdp_url, self.target_id, "PATCH", request_url, body=body or None, headers=headers or None,
                    timeout=timeout or self.timeout, allow_redirects=False)

    def put(self, url: str, *, params: dict[str, Any] | None = None, data: str | bytes | None = None,
            timeout: float | None = None, headers: dict[str, str] | None = None, **_: Any) -> BrowserResponse:
        """Side-effecting PUT in the authenticated tab; the endpoint must be in the write allowlist."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        validate_write_request("PUT", parsed.path, params, headers)
        query = urllib.parse.urlencode({**urllib.parse.parse_qs(parsed.query, keep_blank_values=True), **(params or {})}, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
        return _run(self.cdp_url, self.target_id, "PUT", request_url, body=body or None, headers=headers or None,
                    timeout=timeout or self.timeout, allow_redirects=False)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> BrowserResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        path = parsed.path
        embedded_query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        combined_query: dict[str, Any] = dict(embedded_query)
        for key, value in (params or {}).items():
            combined_query[key] = value
        if "_validated_endpoint" in kwargs:
            endpoint = kwargs.pop("_validated_endpoint")
        else:
            endpoint = validate_request(method, path, combined_query, headers)
        if endpoint.get("method") != "GET":
            raise BridgeError("browser bridge only permits GET reads through request(); use post() for writes")
        query = urllib.parse.urlencode(combined_query, doseq=True)
        request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))
        return _run(
            self.cdp_url,
            self.target_id,
            method.upper(),
            request_url,
            timeout=timeout or self.timeout,
            allow_redirects=allow_redirects,
        )


class OpaqueBrowserSession:
    """Read-only session backed by a caller-owned browser executor.

    The executor receives only an allowlisted method, canonical Ancestry URL,
    timeout, and redirect policy. Authentication remains inside the managed
    browser adapter; this module neither requests nor accepts credential data.
    """

    def __init__(self, executor: BrowserExecutor, timeout: float = 30.0):
        self.executor = executor
        self.timeout = timeout

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        **_: Any,
    ) -> BrowserResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ancestry.com":
            raise BridgeError("browser bridge only permits https://www.ancestry.com requests")
        embedded_query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        combined_query: dict[str, Any] = dict(embedded_query)
        combined_query.update(params or {})
        validate_request("GET", parsed.path, combined_query, headers)
        query = urllib.parse.urlencode(combined_query, doseq=True)
        request_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, "")
        )
        response = self.executor(
            "GET",
            request_url,
            timeout or self.timeout,
            allow_redirects,
        )
        if isinstance(response, BrowserResponse) or all(
            hasattr(response, name) for name in ("status_code", "url", "headers", "text")
        ):
            status_code = response.status_code
            response_url = response.url
            response_headers = response.headers
            response_text = response.text
        elif isinstance(response, dict):
            try:
                status_code = int(response["status_code"])
                response_url = str(response["url"])
                response_headers = dict(response.get("headers") or {})
                response_text = str(response.get("text") or "")
            except (KeyError, TypeError, ValueError) as exc:
                raise BridgeError("opaque browser executor returned an invalid response") from exc
        else:
            raise BridgeError("opaque browser executor returned an invalid response")

        final_url = urllib.parse.urlsplit(response_url)
        if final_url.scheme != "https" or final_url.netloc != "www.ancestry.com":
            raise BridgeError("opaque browser executor returned a non-Ancestry URL")
        if any(_is_sensitive_name(key) for key in urllib.parse.parse_qs(final_url.query)):
            raise BridgeError("opaque browser executor returned a URL with sensitive query material")

        # Browser adapters may expose more response headers than page JavaScript
        # does. Keep only the one header the normalizers use.
        content_type = next(
            (
                str(value)
                for key, value in response_headers.items()
                if str(key).lower() == "content-type"
            ),
            "",
        )
        return BrowserResponse(
            status_code=status_code,
            url=response_url,
            headers={"content-type": content_type} if content_type else {},
            text=response_text,
        )

    def post(self, *_: Any, **__: Any) -> BrowserResponse:
        raise BridgeError("opaque browser transport only permits read-only GETs")


class _CDP:
    def __init__(self, websocket):
        self.websocket = websocket
        self.next_id = 0

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        message: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self.websocket.send(json.dumps(message))
        while True:
            message = json.loads(await self.websocket.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise BridgeError(f"Chrome CDP {method} failed: {message['error']}")
                return message.get("result") or {}


async def _request_async(
    browser_ws: str,
    target: dict[str, Any],
    method: str,
    request_url: str,
    timeout: float,
    allow_redirects: bool,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    body_b64: str | None = None,
) -> BrowserResponse:
    async with websockets.connect(browser_ws, max_size=16 * 1024 * 1024, open_timeout=timeout) as websocket:
        cdp = _CDP(websocket)
        attached = await cdp.call("Target.attachToTarget", {"targetId": target["id"], "flatten": True})
        session_id = attached["sessionId"]
        try:
            expression = json.dumps({
                "url": request_url,
                "method": method,
                "redirect": "follow" if allow_redirects else "manual",
                "body": body,
                "body_b64": body_b64,
                "headers": headers or {},
            })
            js = f"""
                (async () => {{
                  const cfg = {expression};
                  let payload = cfg.body;
                  if (cfg.body_b64) {{
                    const bin = atob(cfg.body_b64);
                    payload = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) payload[i] = bin.charCodeAt(i);
                  }}
                  const response = await fetch(cfg.url, {{
                    method: cfg.method,
                    credentials: 'include',
                    cache: 'no-store',
                    redirect: cfg.redirect,
                    headers: Object.assign({{'Accept': 'application/json, text/plain, */*'}}, cfg.headers),
                    body: (cfg.method === 'GET' || !payload) ? undefined : payload
                  }});
                  return {{
                    status: response.status,
                    url: response.url,
                    headers: Object.fromEntries(response.headers.entries()),
                    body: await response.text()
                  }};
                }})()
            """
            result = await cdp.call("Runtime.evaluate", {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": False,
            }, session_id=session_id)
            if result.get("exceptionDetails"):
                raise BridgeError("page-context fetch raised an exception")
            value = ((result.get("result") or {}).get("value") or {})
            if value.get("error"):
                raise BridgeError(str(value["error"]))
            if "status" not in value:
                raise BridgeError("page-context fetch returned no HTTP status")
            return BrowserResponse(
                status_code=int(value["status"]),
                url=str(value.get("url") or request_url),
                headers={str(k).lower(): str(v) for k, v in (value.get("headers") or {}).items()},
                text=str(value.get("body") or ""),
            )
        finally:
            try:
                await cdp.call("Target.detachFromTarget", {"sessionId": session_id})
            except Exception:
                pass


def _run(
    cdp_url: str,
    target_id: str | None,
    method: str,
    request_url: str,
    *,
    timeout: float,
    allow_redirects: bool,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    body_b64: str | None = None,
) -> BrowserResponse:
    target = _target_for(cdp_url, target_id)
    version = _cdp_json(cdp_url, "/json/version")
    browser_ws = version.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BridgeError("Chrome CDP did not expose a browser WebSocket")
    return asyncio.run(_request_async(
        browser_ws, target, method, request_url, timeout, allow_redirects, body=body, headers=headers,
        body_b64=body_b64))


async def _image_async(browser_ws, target, collection, image_id, record_id, scale, timeout):
    async with websockets.connect(browser_ws, max_size=64 * 1024 * 1024, open_timeout=timeout) as websocket:
        cdp = _CDP(websocket)
        session_id = (await cdp.call("Target.attachToTarget", {"targetId": target["id"], "flatten": True}))["sessionId"]
        try:
            viewer = f"/imageviewer/collections/{collection}/images/{image_id}" + (f"?pId={record_id}" if record_id else "")
            js = """(async()=>{const cfg=%s;
              const page=await (await fetch(cfg.viewer,{credentials:'include'})).text();
              const re=new RegExp('/api/media/retrieval/v2/image/namespaces/'+cfg.collection+'/media/'+cfg.image+'[.]jpg[?]securitytoken=[A-Za-z0-9]+');
              const m=page.match(re); if(!m) return {error:'image-url-not-found'};
              const r=await fetch(m[0]+'&download=false&client=imageviewer-ui&imagequality=HighQuality&scale='+cfg.scale,{credentials:'include'});
              const b=await r.blob(); const fr=new FileReader();
              const b64=await new Promise(res=>{fr.onload=()=>res(fr.result.split(',')[1]);fr.readAsDataURL(b)});
              return {status:r.status,type:r.headers.get('content-type'),b64:b64}})()""" % json.dumps(
                {"viewer": viewer, "collection": str(collection), "image": image_id, "scale": str(scale)})
            result = await cdp.call("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True,
                                                         "userGesture": False}, session_id=session_id)
            if result.get("exceptionDetails"):
                raise BridgeError("page-context image fetch raised an exception")
            return (result.get("result") or {}).get("value") or {}
        finally:
            try:
                await cdp.call("Target.detachFromTarget", {"sessionId": session_id})
            except Exception:
                pass


def fetch_record_image(cdp_url, target_id, collection, image_id, record_id=None, scale=1, timeout=60.0):
    """Read-only: fetch one record image through the signed-in tab. The image URL's security token is read from the viewer
    page and used inside the page; it never leaves the browser. Returns (status, content_type, bytes)."""
    import base64
    if not re.fullmatch(r"[0-9]{1,9}", str(collection)) or not re.fullmatch(r"[0-9A-Za-z_\-]{1,64}", str(image_id)) \
            or (record_id and not re.fullmatch(r"[0-9]{1,15}", str(record_id))) or not re.fullmatch(r"[0-9](\.[0-9]+)?", str(scale)):
        raise BridgeError("invalid image arguments")
    target = _target_for(cdp_url, target_id)
    browser_ws = _cdp_json(cdp_url, "/json/version").get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BridgeError("Chrome CDP did not expose a browser WebSocket")
    value = asyncio.run(_image_async(browser_ws, target, collection, image_id, record_id, scale, timeout))
    if value.get("error"):
        raise BridgeError(str(value["error"]))
    return int(value.get("status") or 0), str(value.get("type") or ""), base64.b64decode(value.get("b64") or "")


def summarize_response(response: BrowserResponse) -> dict[str, Any]:
    """Return safe response-shape evidence without returning the body."""

    summary: dict[str, Any] = {
        "status": response.status_code,
        "url_path": urllib.parse.urlsplit(response.url).path,
        "content_type": response.headers.get("content-type", "")[:100],
        "bytes": len(response.content),
    }
    try:
        payload = response.json()
        if isinstance(payload, dict):
            summary["json_top_keys"] = sorted(str(k) for k in payload)[:80]
        elif isinstance(payload, list):
            summary["json_shape"] = f"list[{len(payload)}]"
        else:
            summary["json_shape"] = type(payload).__name__
    except (TypeError, ValueError):
        if "html" in summary["content_type"].lower() or response.text.lstrip().startswith("<"):
            summary["body_shape"] = "html"
            summary["contains_person_data"] = "person-data" in response.text or "researchData" in response.text
    return summary


def _parse_query(values: list[str]) -> dict[str, str]:
    query: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise BridgeError(f"query must be KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        query[key] = value
    return query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="allowlisted Ancestry path, without query string")
    parser.add_argument("--cdp-url", default=os.environ.get("ANCESTRY_CDP_URL", DEFAULT_CDP_URL))
    parser.add_argument("--target-id", default=os.environ.get("ANCESTRY_CDP_TARGET_ID"))
    parser.add_argument("--query", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--raw", action="store_true", help="print the response body; keep output local")
    parser.add_argument("--diagnose", action="store_true", help="report browser transport capability only")
    args = parser.parse_args()
    if args.diagnose:
        diagnosis = diagnose_cdp(args.cdp_url)
        print(json.dumps(diagnosis))
        return 0 if diagnosis["ok"] else 2
    if not args.path:
        parser.error("path is required unless --diagnose is used")
    try:
        response = ChromeBrowserSession(args.cdp_url, args.target_id, args.timeout).get(
            BASE + args.path,
            params=_parse_query(args.query),
            timeout=args.timeout,
        )
        output: dict[str, Any] = summarize_response(response)
        if args.raw:
            output["body"] = response.text
        print(json.dumps(output, ensure_ascii=False))
        return 0 if response.ok else 1
    except BridgeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
