"""Tests for HTTP uptime probing → probe_up/probe_latency_ms gauges."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import uptime_prober as up


@pytest.fixture(autouse=True)
def _no_real_tls():
    """默认屏蔽真实 TLS 握手(否则拨测路径会真连网)。TLS 专项测试自行覆盖。
    同时中和 SSRF 守卫的真实 DNS 解析(这些用例用不可解析的假主机,SSRF 行为在
    test_ssrf.py 专项覆盖)。"""
    safe = SimpleNamespace(is_safe=True, reason="", resolved_ips=[], failed_check=None)
    with (
        patch.object(up, "_tls_days_remaining", return_value=None),
        patch("aegis.server.lib.ssrf.url_safety_check", return_value=safe),
    ):
        yield


def _conn_one_target():
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid.uuid4(),
                "org_id": uuid.uuid4(),
                "name": "helios",
                "url": "https://helios.example/health",
                "expected_status": 200,
            }
        ]
    )
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_records_up_when_healthy():
    conn = _conn_one_target()
    res = SimpleNamespace(healthy=True, status_code=200, elapsed_ms=42, error=None)
    with patch.object(up, "network_http_health", return_value=res):
        n = await up.probe_due_targets(conn)
    assert n == 1
    rows = conn.executemany.await_args.args[1]
    by_metric = {r[1]: r[2] for r in rows}
    assert by_metric["probe_up"] == 1.0
    assert by_metric["probe_latency_ms"] == 42.0
    # target row updated up=True
    assert conn.execute.await_args.args[1] is True


@pytest.mark.asyncio
async def test_records_down_when_unhealthy():
    conn = _conn_one_target()
    res = SimpleNamespace(healthy=False, status_code=503, elapsed_ms=10, error="503")
    with patch.object(up, "network_http_health", return_value=res):
        await up.probe_due_targets(conn)
    rows = conn.executemany.await_args.args[1]
    assert {r[1]: r[2] for r in rows}["probe_up"] == 0.0


@pytest.mark.asyncio
async def test_probe_exception_is_down_not_crash():
    conn = _conn_one_target()
    with patch.object(up, "network_http_health", side_effect=RuntimeError("dns fail")):
        n = await up.probe_due_targets(conn)
    assert n == 1  # did not raise
    assert {r[1]: r[2] for r in conn.executemany.await_args.args[1]}["probe_up"] == 0.0


@pytest.mark.asyncio
async def test_records_tls_cert_days_when_https():
    """§3.2: HTTPS 目标顺带记 tls_cert_days_remaining gauge。"""
    conn = _conn_one_target()
    res = SimpleNamespace(healthy=True, status_code=200, elapsed_ms=5, error=None)
    with (
        patch.object(up, "network_http_health", return_value=res),
        patch.object(up, "_tls_days_remaining", return_value=42.0),
    ):
        await up.probe_due_targets(conn)
    by_metric = {r[1]: r[2] for r in conn.executemany.await_args.args[1]}
    assert by_metric["tls_cert_days_remaining"] == 42.0


@pytest.mark.asyncio
async def test_no_tls_metric_when_probe_returns_none():
    """非 https/握手失败(None)→ 不记 tls gauge。"""
    conn = _conn_one_target()
    res = SimpleNamespace(healthy=True, status_code=200, elapsed_ms=5, error=None)
    with (
        patch.object(up, "network_http_health", return_value=res),
        patch.object(up, "_tls_days_remaining", return_value=None),
    ):
        await up.probe_due_targets(conn)
    metrics = {r[1] for r in conn.executemany.await_args.args[1]}
    assert "tls_cert_days_remaining" not in metrics


@pytest.mark.asyncio
async def test_expiring_cert_logs_warning():
    conn = _conn_one_target()
    res = SimpleNamespace(healthy=True, status_code=200, elapsed_ms=5, error=None)
    with (
        patch.object(up, "network_http_health", return_value=res),
        patch.object(up, "_tls_days_remaining", return_value=3.0),
        patch.object(up.log, "warning") as m_warn,
    ):
        await up.probe_due_targets(conn)
    assert any("tls_cert_expiring" in str(c.args) for c in m_warn.call_args_list)


def test_tls_days_remaining_non_https_returns_none():
    assert up._tls_days_remaining("http://plain.example/x") is None
    assert up._tls_days_remaining("ftp://x") is None
