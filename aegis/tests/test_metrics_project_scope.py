"""B2: project dimension for metrics — scrape targets stamp `project_id` into
sample tags, and project-scoped alert rules only evaluate their own series."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest import mock

import pytest

from aegis.server.orchestration import alert_evaluation
from aegis.server.services import metrics_scraper

_PROJ = uuid.UUID("33333333-3333-3333-3333-333333333333")
_OTHER = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _rule(operator: str = ">") -> mock.MagicMock:
    return mock.MagicMock(
        metric="cpu_percent", operator=operator, project_id=_PROJ, name="r", org_id=uuid.uuid4()
    )


# ── scraper stamping ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_stamps_project_id_into_tags() -> None:
    conn = mock.AsyncMock()
    n = await metrics_scraper._store(
        conn,
        hostname="node1",
        samples=[("cpu_percent", 9.0, {"core": "0"})],
        static_labels={"env": "prod"},
        project_id=_PROJ,
    )
    assert n == 1
    tags = json.loads(conn.executemany.await_args.args[1][0][4])
    assert tags == {"core": "0", "env": "prod", "project_id": str(_PROJ)}


@pytest.mark.asyncio
async def test_store_without_project_leaves_tags_untagged() -> None:
    """Shared/infra targets must stay unattributed so every project still sees them."""
    conn = mock.AsyncMock()
    await metrics_scraper._store(
        conn,
        hostname="node1",
        samples=[("cpu_percent", 9.0, {})],
        static_labels={},
        project_id=None,
    )
    assert json.loads(conn.executemany.await_args.args[1][0][4]) == {}


# ── rule evaluation filtering ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_query_filters_by_project_and_keeps_shared() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"value": 5.0, "hostname": "h"}]
    await alert_evaluation._worst_series_for_rule(conn, _rule(), since=datetime.now(UTC))
    sql, metric, _since, project = conn.fetch.await_args.args
    assert "tags->>'project_id' IS NULL OR tags->>'project_id' = $3" in sql
    assert metric == "cpu_percent"
    assert project == str(_PROJ)


@pytest.mark.asyncio
async def test_current_value_query_is_project_scoped() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"value": 1.0}, {"value": 7.0}]
    val = await alert_evaluation._current_value_for_rule(conn, _rule(), since=datetime.now(UTC))
    assert val == 7.0  # '>' takes the worst (max) series
    assert conn.fetch.await_args.args[3] == str(_PROJ)


@pytest.mark.asyncio
async def test_other_projects_series_are_excluded_end_to_end() -> None:
    """Simulate the DB honoring the filter: rows of a foreign project are not
    returned, so the rule sees no signal instead of firing on someone else's data."""
    rows_by_project = {str(_PROJ): [], str(_OTHER): [{"value": 99.0, "hostname": "h"}]}

    async def fake_fetch(_sql, _metric, _since, project):
        return rows_by_project[project]

    conn = mock.AsyncMock()
    conn.fetch.side_effect = fake_fetch
    assert (
        await alert_evaluation._worst_series_for_rule(conn, _rule(), since=datetime.now(UTC))
        is None
    )
