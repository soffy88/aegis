"""SSRF-safe webhook POST with vetted-IP pinning.

Closes the check→connect TOCTOU that a plain ``guard_external`` + separate fetch
leaves open: DNS is resolved and validated once (``url_safety_check`` via
:func:`aegis.server.lib.ssrf.guard_external`), and the connection is then made
directly to one of the *vetted* IPs — the HTTP client never re-resolves the
hostname, so a rebinding attack between check and connect has nothing to bind.

TLS keeps verifying against the original hostname: the pinned connection passes
``server_hostname=<original host>`` to ``ssl``, so SNI + certificate verification
are unchanged; only the TCP destination is pinned.

Why not oprim.http_post_webhook?  Its httpx client resolves the hostname itself
with no transport injection point, so pinning is impossible through it. This is
a deliberate, scoped deviation (webhook delivery only); HMAC signing stays on
``obase.webhook.sign_payload`` (see webhook_dispatcher).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

from aegis.server.lib.ssrf import SSRFBlocked, guard_external

MAX_RESPONSE_BODY_BYTES = 4096
_ALLOWED_SCHEMES = ("http", "https")


class WebhookResult:
    """Mirror of oprim.WebhookResult's shape (dispatcher only reads attributes).

    success=False + status_code=None ⇒ request never completed (blocked/network).
    """

    def __init__(
        self,
        *,
        success: bool,
        status_code: int | None,
        elapsed_ms: float,
        response_body: str,
        error: str | None,
    ) -> None:
        self.success = success
        self.status_code = status_code
        self.elapsed_ms = elapsed_ms
        self.response_body = response_body
        self.error = error


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose TCP destination is a pre-vetted IP."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant — SNI + cert verification still use the ORIGINAL hostname."""

    def __init__(
        self, host: str, port: int, pinned_ip: str, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._ssl_ctx = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # server_hostname=<original host> ⇒ SNI + hostname verification target the
        # URL's host, not the pinned IP. check_hostname is enforced by the context.
        self.sock = self._ssl_ctx.wrap_socket(sock, server_hostname=self.host)


def _make_connection(url: str, pinned_ip: str, timeout: float) -> http.client.HTTPConnection:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        ctx = ssl.create_default_context()
        return _PinnedHTTPSConnection(parts.hostname or "", port, pinned_ip, timeout, ctx)
    return _PinnedHTTPConnection(parts.hostname or "", port, pinned_ip, timeout)


def http_post_webhook(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_sec: float = 10.0,
    signature: str | None = None,
    signature_header: str = "X-Aegis-Signature",
    user_agent: str = "Aegis-Webhook/1.0",
) -> WebhookResult:
    """Single HTTP POST webhook delivery with SSRF IP pinning.

    Never raises (mirrors oprim.http_post_webhook contract). Resolution + connect
    happen in the same call, so no DNS rebinding window exists. Redirects are not
    followed (a 3xx is surfaced as a status code, matching oprim).
    """
    started = time.monotonic()

    def done(status: int | None, body: str, error: str | None) -> WebhookResult:
        return WebhookResult(
            success=status is not None and 200 <= status < 300,
            status_code=status,
            elapsed_ms=(time.monotonic() - started) * 1000,
            response_body=body,
            error=error,
        )

    try:
        payload_json = json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        return done(None, "", f"payload_not_serializable: {exc}")

    try:
        # Resolve + validate + pin in one step. Returns vetted IPs; raises SSRFBlocked.
        vetted_ips = guard_external(url)
        if not vetted_ips:
            return done(None, "", "blocked: url resolved to no addresses")
    except SSRFBlocked as exc:
        return done(None, "", f"blocked: {exc}")

    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        return done(None, "", f"blocked: unsupported url {url!r}")

    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }
    if headers:
        request_headers.update(headers)
    if signature:
        request_headers[signature_header] = signature

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    # Fail over across vetted addresses only while *establishing* the connection
    # (connect() errors are unambiguous — nothing was sent). Once a connection is
    # up we never retry, so a request can't be double-delivered.
    connect_errors: list[str] = []
    for ip in vetted_ips:
        try:
            conn = _make_connection(url, ip, timeout_sec)
        except (ValueError, OSError) as exc:
            connect_errors.append(f"{ip}: {exc}")
            continue
        try:
            conn.connect()
        except (TimeoutError, OSError) as exc:
            # connect-level failure only — TLS errors (SSLError) are NOT a failover
            # trigger: the same cert serves every address, so a verify failure on
            # one IP is a verify failure on all (and must be surfaced as-is).
            conn.close()
            connect_errors.append(f"{ip}: {exc}")
            continue
        break
    else:
        return done(None, "", "connect_failed:" + ("; ".join(connect_errors) or "no addresses"))

    try:
        conn.request("POST", path, body=payload_json, headers=request_headers)
        resp = conn.getresponse()
        body = resp.read(MAX_RESPONSE_BODY_BYTES).decode("utf-8", "replace")
        return done(resp.status, body, None)
    except (TimeoutError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
        return done(None, "", f"connect_failed:{exc}")
    finally:
        conn.close()
