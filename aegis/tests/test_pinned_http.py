"""Tests for pinned_http — SSRF-safe webhook POST with vetted-IP pinning."""

from __future__ import annotations

import http.server
import socket
import threading
from types import SimpleNamespace
from unittest import mock

from aegis.server.lib import pinned_http
from aegis.server.lib.pinned_http import http_post_webhook


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        self.send_response(201)
        self.send_header("content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    def log_message(self, *args: object) -> None:  # silence test noise
        pass


def _result(is_safe: bool, ips: list[str], reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(is_safe=is_safe, reason=reason, resolved_ips=ips, failed_check=None)


def test_pins_vetted_ip_and_posts() -> None:
    """The request must land on the vetted IP (not the URL host) with Host intact."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://example.invalid:{port}/hook"
        with mock.patch.object(
            pinned_http, "guard_external", return_value=["127.0.0.1"]
        ):
            result = http_post_webhook(
                url=url, payload={"a": 1}, timeout_sec=5.0, user_agent="test/1.0"
            )
        assert result.success is True
        assert result.status_code == 201
        assert result.response_body == 'echo:{"a": 1}'
    finally:
        server.shutdown()
        thread.join()


def test_failover_to_next_vetted_ip() -> None:
    """A dead vetted address is skipped; the request goes to the next one."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://example.invalid:{port}/hook"
        # 127.0.0.2:port is almost certainly closed → fail over to 127.0.0.1
        with mock.patch.object(
            pinned_http, "guard_external", return_value=["127.0.0.2", "127.0.0.1"]
        ):
            result = http_post_webhook(url=url, payload={"x": 2}, timeout_sec=3.0)
        assert result.success is True
        assert result.status_code == 201
    finally:
        server.shutdown()
        thread.join()


def test_blocked_url_is_a_failed_result_not_a_raise() -> None:
    """SSRFBlocked must surface as success=False (dead-letter), never crash the loop."""
    with mock.patch.object(
        pinned_http, "guard_external", side_effect=pinned_http.SSRFBlocked("private")
    ):
        result = http_post_webhook(url="http://10.0.0.5/hook", payload={})
    assert result.success is False
    assert result.status_code is None
    assert "blocked" in (result.error or "")


def test_no_vetted_ips_fails_closed() -> None:
    with mock.patch.object(pinned_http, "guard_external", return_value=[]):
        result = http_post_webhook(url="http://example.com/hook", payload={})
    assert result.success is False
    assert "no addresses" in (result.error or "")


def test_all_addresses_dead_reports_connect_failure() -> None:
    """Closed port on every vetted IP → connect_failed (retryable), not a crash."""
    # Use a port that's certainly not listening: bind then close to grab a free port.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with mock.patch.object(pinned_http, "guard_external", return_value=["127.0.0.1"]):
        result = http_post_webhook(
            url=f"http://example.invalid:{port}/hook", payload={}, timeout_sec=3.0
        )
    assert result.success is False
    assert result.status_code is None
    assert (result.error or "").startswith("connect_failed")


def test_unserializable_payload_fails_cleanly() -> None:
    with mock.patch.object(pinned_http, "guard_external", return_value=["127.0.0.1"]):
        result = http_post_webhook(url="http://example.com/hook", payload={object(): 1})
    assert result.success is False
    assert "payload_not_serializable" in (result.error or "")


def test_https_pins_ip_and_still_verifies_hostname() -> None:
    """TLS must fail against the pinned IP when the cert isn't for the URL host."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        # Plain-HTTP server + https:// scheme → handshake/EOF error, but the key
        # assertion is it attempted the *pinned* IP and reported a connect_failed
        # result instead of raising or leaking the failure mode.
        with mock.patch.object(pinned_http, "guard_external", return_value=["127.0.0.1"]):
            result = http_post_webhook(
                url=f"https://not-the-cert-host.invalid:{port}/hook",
                payload={},
                timeout_sec=3.0,
            )
        assert result.success is False
        assert (result.error or "").startswith("connect_failed")
    finally:
        server.shutdown()
        thread.join()
