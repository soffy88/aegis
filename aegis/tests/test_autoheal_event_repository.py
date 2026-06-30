"""Tests for AutoHealEventRepository + the alert-fire → event writer (audit P0 #3).

The aegis_alert_events table previously had no writer; these verify the repo's
insert/get/handle/stats paths and that a fired alert records an event.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.engines.alert_engine import AlertEvaluationResult
from aegis.server.orchestration import alert_evaluation as ae
from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository
from aegis.server.schemas.alerting import AlertRuleResponse

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_insert_passes_columns():
    conn = MagicMock()
    new_id = uuid.uuid4()
    conn.fetchrow = AsyncMock(return_value={"id": new_id})
    repo = AutoHealEventRepository(conn)

    got = await repo.insert(
        org_id=_ORG, cycle_id=uuid.uuid4(), severity="critical",
        source="alert_rule:cpu", reason="95 >= 90", value=95.0,
    )

    assert got == new_id
    args = conn.fetchrow.await_args.args
    # params: ($1 cycle_id, $2 severity, $3 source, $4 reason, $5 value, $6 org_id)
    assert "INSERT INTO aegis_alert_events" in args[0]
    assert args[2] == "critical" and args[3] == "alert_rule:cpu" and args[5] == 95.0


@pytest.mark.asyncio
async def test_mark_handled_false_when_missing():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    repo = AutoHealEventRepository(conn)
    assert await repo.mark_handled(org_id=_ORG, event_id=uuid.uuid4()) is False

    conn.execute = AsyncMock(return_value="UPDATE 1")
    assert await repo.mark_handled(org_id=_ORG, event_id=uuid.uuid4()) is True


@pytest.mark.asyncio
async def test_fired_alert_writes_autoheal_event():
    """A new fire must record an aegis_alert_events row carrying severity/value."""
    rule = AlertRuleResponse.model_validate(dict(
        rule_id=uuid.uuid4(), org_id=_ORG, project_id=_PROJ, name="cpu-high",
        metric="cpu_percent", threshold_warn=70.0, threshold_critical=90.0,
        operator=">=", throttle_seconds=300, escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600, enabled=True, created_by=uuid.uuid4(),
        created_at=_NOW, updated_at=_NOW,
    ))
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"value": 95.0}])

    async def fake_eval(*, rule, current_value, now):
        return AlertEvaluationResult(
            rule_id=rule.rule_id, fired=True, throttled=False, dedup_existed=False,
            severity="critical", fired_row=None, reason="95.0 >= 90.0",
        )

    insert_mock = AsyncMock(return_value=uuid.uuid4())
    with patch.object(ae.AlertRuleRepository, "list_all_enabled",
                      AsyncMock(return_value=[rule])), \
         patch.object(ae.AlertEngine, "evaluate_metric", side_effect=fake_eval), \
         patch.object(ae.AutoHealEventRepository, "insert", insert_mock):
        stats = await ae.run_alert_evaluation(conn=conn, now=_NOW)

    assert stats["fired"] == 1
    insert_mock.assert_awaited_once()
    kw = insert_mock.await_args.kwargs
    assert kw["org_id"] == _ORG and kw["severity"] == "critical"
    assert kw["source"] == "alert_rule:cpu-high" and kw["value"] == 95.0


@pytest.mark.asyncio
async def test_no_event_written_when_not_fired():
    rule = AlertRuleResponse.model_validate(dict(
        rule_id=uuid.uuid4(), org_id=_ORG, project_id=_PROJ, name="cpu-high",
        metric="cpu_percent", threshold_warn=70.0, threshold_critical=90.0,
        operator=">=", throttle_seconds=300, escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600, enabled=True, created_by=uuid.uuid4(),
        created_at=_NOW, updated_at=_NOW,
    ))
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"value": 10.0}])

    async def fake_eval(*, rule, current_value, now):
        return AlertEvaluationResult(
            rule_id=rule.rule_id, fired=False, throttled=False, dedup_existed=False,
            severity="ok", fired_row=None, reason="ok",
        )

    insert_mock = AsyncMock()
    with patch.object(ae.AlertRuleRepository, "list_all_enabled",
                      AsyncMock(return_value=[rule])), \
         patch.object(ae.AlertEngine, "evaluate_metric", side_effect=fake_eval), \
         patch.object(ae.AutoHealEventRepository, "insert", insert_mock):
        await ae.run_alert_evaluation(conn=conn, now=_NOW)

    insert_mock.assert_not_awaited()
