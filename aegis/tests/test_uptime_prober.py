"""Tests for HTTP uptime probing → probe_up/probe_latency_ms gauges."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import uptime_prober as up


def _conn_one_target():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{
        "id": uuid.uuid4(), "org_id": uuid.uuid4(), "name": "helios",
        "url": "https://helios.example/health", "expected_status": 200,
    }])
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
