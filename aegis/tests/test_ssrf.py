"""Tests for the SSRF guards (aegis.server.lib.ssrf)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from aegis.server.lib import ssrf


def _result(is_safe: bool, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(is_safe=is_safe, reason=reason, resolved_ips=[], failed_check=None)


def test_guard_external_blocks_private() -> None:
    with mock.patch.object(
        ssrf, "url_safety_check", return_value=_result(False, "is_private_blocked")
    ) as chk:
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.guard_external("http://10.0.0.5/hook")
    # strict preset: loopback + private both blocked
    assert chk.call_args.kwargs["block_private"] is True
    assert chk.call_args.kwargs["block_loopback"] is True


def test_guard_external_allows_public() -> None:
    with mock.patch.object(ssrf, "url_safety_check", return_value=_result(True)):
        ssrf.guard_external("https://hooks.slack.com/services/x")  # no raise


def test_guard_scrape_allows_private_but_blocks_metadata() -> None:
    # scrape preset must NOT block private/loopback (legit exporters) ...
    with mock.patch.object(ssrf, "url_safety_check", return_value=_result(True)) as chk:
        ssrf.guard_scrape("http://10.0.0.9:9100/metrics")
    assert chk.call_args.kwargs["block_private"] is False
    assert chk.call_args.kwargs["block_loopback"] is False
    # ... but always blocks link-local (169.254.169.254 metadata) + reserved + multicast
    assert chk.call_args.kwargs["block_link_local"] is True
    assert chk.call_args.kwargs["block_reserved"] is True

    with mock.patch.object(
        ssrf, "url_safety_check", return_value=_result(False, "is_link_local_blocked")
    ):
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.guard_scrape("http://169.254.169.254/latest/meta-data/")


def test_url_safety_error_is_wrapped() -> None:
    with mock.patch.object(ssrf, "url_safety_check", side_effect=ssrf.URLSafetyError("boom")):
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.guard_external("http://[::bad")


def test_channel_send_guards_user_url() -> None:
    """_send must SSRF-check the slack/webhook URL before posting."""
    from aegis.server.api.routers import channels

    with (
        mock.patch.object(channels, "httpx") as _httpx,
        mock.patch(
            "aegis.server.lib.ssrf.url_safety_check",
            return_value=_result(False, "is_loopback_blocked"),
        ),
    ):
        with pytest.raises(ssrf.SSRFBlocked):
            channels._send("webhook", {"url": "http://127.0.0.1:2019/config"}, "hi")
    _httpx.post.assert_not_called()  # blocked before any request
