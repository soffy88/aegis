"""Tests for the Prometheus scrape service."""

from __future__ import annotations

import uuid
from unittest import mock

import httpx
import pytest

from aegis.server.services import metrics_scraper

_PROM = 'node_up 1\nhttp_requests_total{code="200"} 5\n'


@pytest.fixture(autouse=True)
def _allow_ssrf_guard():
    """These tests use non-resolvable fake hosts and exercise parsing, not the SSRF
    guard (covered by test_ssrf.py) — neutralize the guard's real DNS lookup."""
    from types import SimpleNamespace

    safe = SimpleNamespace(is_safe=True, reason="", resolved_ips=[], failed_check=None)
    with mock.patch("aegis.server.lib.ssrf.url_safety_check", return_value=safe):
        yield


def _httpx_returning(text: str, status_code: int = 200):
    """Patch httpx.AsyncClient so .get() returns a canned response."""
    resp = mock.MagicMock(status_code=status_code, text=text)
    client = mock.MagicMock()
    client.get = mock.AsyncMock(return_value=resp)
    cm = mock.MagicMock()
    cm.__aenter__ = mock.AsyncMock(return_value=client)
    cm.__aexit__ = mock.AsyncMock(return_value=False)
    return mock.patch.object(httpx, "AsyncClient", return_value=cm)


@pytest.mark.asyncio
async def test_scrape_url_parses_samples() -> None:
    with _httpx_returning(_PROM):
        samples = await metrics_scraper.scrape_url("http://x/metrics")
    names = [s[0] for s in samples]
    assert "node_up" in names and "http_requests_total" in names


@pytest.mark.asyncio
async def test_scrape_url_raises_on_non_200() -> None:
    with _httpx_returning("nope", status_code=503):
        with pytest.raises(ValueError, match="HTTP 503"):
            await metrics_scraper.scrape_url("http://x/metrics")


@pytest.mark.asyncio
async def test_scrape_due_targets_stores_and_marks_ok() -> None:
    tid = uuid.uuid4()
    conn = mock.AsyncMock()
    conn.fetch.return_value = [
        {"id": tid, "name": "node", "url": "http://x/metrics", "interval_seconds": 30, "labels": {}}
    ]
    with _httpx_returning(_PROM):
        summary = await metrics_scraper.scrape_due_targets(conn)
    assert summary == {"scraped": 1, "failed": 0, "samples": 2}
    # bulk insert happened
    assert conn.executemany.await_count == 1
    # target marked ok
    update_sqls = " ".join(c.args[0] for c in conn.execute.await_args_list)
    assert "last_status" in update_sqls and "last_error = NULL" in update_sqls


@pytest.mark.asyncio
async def test_scrape_due_targets_records_failure_and_continues() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [
        {
            "id": uuid.uuid4(),
            "name": "bad",
            "url": "http://x/metrics",
            "interval_seconds": 30,
            "labels": {},
        }
    ]
    with _httpx_returning("x", status_code=500):
        summary = await metrics_scraper.scrape_due_targets(conn)
    assert summary == {"scraped": 0, "failed": 1, "samples": 0}
    conn.executemany.assert_not_called()  # nothing stored
    update_sqls = " ".join(c.args[0] for c in conn.execute.await_args_list)
    assert "last_status = 'error'" in update_sqls


@pytest.mark.asyncio
async def test_static_labels_merged_into_samples() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [
        {
            "id": uuid.uuid4(),
            "name": "node",
            "url": "http://x/metrics",
            "interval_seconds": 30,
            "labels": {"env": "prod"},
        }
    ]
    with _httpx_returning("up 1\n"):
        await metrics_scraper.scrape_due_targets(conn)
    rows = conn.executemany.await_args.args[1]
    # tags json should include the static label
    assert any('"env": "prod"' in r[4] for r in rows)
