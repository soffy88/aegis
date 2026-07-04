"""Tests for the alert rule evaluation driver (audit P0 #2).

Verifies that enabled rules are swept, the worst-host value is fed to the engine
per operator direction, rules with no fresh data are skipped, and a fire bubbles
into the stats.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.engines.alert_engine import AlertEvaluationResult
from aegis.server.orchestration import alert_evaluation as ae
from aegis.server.schemas.alerting import AlertRuleResponse

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _rule(**kw) -> AlertRuleResponse:
    base = dict(
        rule_id=uuid.uuid4(),
        org_id=_ORG,
        project_id=_PROJ,
        name="cpu-high",
        metric="cpu_percent",
        threshold_warn=70.0,
        threshold_critical=90.0,
        operator=">=",
        throttle_seconds=300,
        escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600,
        enabled=True,
        created_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(kw)
    return AlertRuleResponse.model_validate(base)


def _conn_with_values(values: list[float]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"value": v} for v in values])
    return conn


@pytest.mark.asyncio
async def test_aggregates_per_series_not_per_host():
    """The metric query must dedup per (hostname, tags) so multi-container
    Prometheus metrics (all under one hostname, e.g. cAdvisor) are each counted."""
    conn = _conn_with_values([1.0, 2.0])
    await ae._current_value_for_rule(conn, _rule(operator=">="), since=_NOW)
    sql = conn.fetch.await_args.args[0]
    assert "DISTINCT ON (hostname, tags)" in sql
    assert "ORDER BY hostname, tags, ts DESC" in sql


@pytest.mark.asyncio
async def test_picks_max_for_greater_operator():
    """For >=, the worst (max) per-host value drives the decision."""
    captured = {}

    async def fake_eval(*, rule, current_value, now):
        captured["value"] = current_value
        return AlertEvaluationResult(
            rule_id=rule.rule_id, fired=True, throttled=False,
            dedup_existed=False, severity="critical", fired_row=None, reason="x",
        )

    conn = _conn_with_values([40.0, 95.0, 60.0])
    with patch.object(ae.AlertRuleRepository, "list_all_enabled",
                      AsyncMock(return_value=[_rule(operator=">=")])), \
         patch.object(ae.AlertEngine, "evaluate_metric", side_effect=fake_eval), \
         patch.object(ae.AutoHealEventRepository, "insert", AsyncMock()):
        stats = await ae.run_alert_evaluation(conn=conn, now=_NOW)

    assert captured["value"] == 95.0
    assert stats == {"evaluated": 1, "fired": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_picks_min_for_less_operator():
    """For <=, the worst (min) per-host value drives the decision."""
    captured = {}

    async def fake_eval(*, rule, current_value, now):
        captured["value"] = current_value
        return AlertEvaluationResult(
            rule_id=rule.rule_id, fired=False, throttled=False,
            dedup_existed=False, severity="ok", fired_row=None, reason="ok",
        )

    conn = _conn_with_values([40.0, 95.0, 60.0])
    with patch.object(ae.AlertRuleRepository, "list_all_enabled",
                      AsyncMock(return_value=[_rule(operator="<=", threshold_critical=10.0,
                                                    threshold_warn=None)])), \
         patch.object(ae.AlertEngine, "evaluate_metric", side_effect=fake_eval):
        await ae.run_alert_evaluation(conn=conn, now=_NOW)

    assert captured["value"] == 40.0


@pytest.mark.asyncio
async def test_skips_rule_with_no_recent_data():
    """A rule whose metric has no fresh reading is skipped, not evaluated."""
    conn = _conn_with_values([])  # empty
    eval_mock = AsyncMock()
    with patch.object(ae.AlertRuleRepository, "list_all_enabled",
                      AsyncMock(return_value=[_rule()])), \
         patch.object(ae.AlertEngine, "evaluate_metric", eval_mock):
        stats = await ae.run_alert_evaluation(conn=conn, now=_NOW)

    eval_mock.assert_not_awaited()
    assert stats == {"evaluated": 0, "fired": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_loop_registered_in_cron_main():
    from aegis.server.orchestration import cron

    scheduled: list[str] = []

    async def _fake_gather(*coros, **_kw):
        for c in coros:
            scheduled.append(getattr(c, "__name__", str(c)))
            c.close()

    with patch.object(cron.asyncio, "gather", side_effect=_fake_gather), patch.object(
        cron, "_acquire_loop_runner_role", AsyncMock(return_value=AsyncMock())
    ):
        await cron._cron_main(alerter=None)

    assert "_alert_eval_loop" in scheduled
