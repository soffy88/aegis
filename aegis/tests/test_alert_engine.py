"""Tests for AlertEngine — C2-2. Pure unit tests (no DB, mocked repos + oprim)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aegis.server.engines.alert_engine import AlertEngine
from aegis.server.schemas.alerting import AlertFiredResponse, AlertRuleResponse

_ORG = uuid.UUID("55555555-5555-5555-5555-555555555555")
_PROJ = uuid.UUID("66666666-6666-6666-6666-666666666666")
_USER = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_RULE_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _rule(**kwargs: object) -> AlertRuleResponse:
    defaults: dict = dict(
        rule_id=_RULE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        name="cpu-rule",
        metric="container.cpu.percent",
        threshold_warn=70.0,
        threshold_critical=90.0,
        operator=">=",
        throttle_seconds=300,
        escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600,
        enabled=True,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    return AlertRuleResponse.model_validate(defaults)


def _fired(
    severity: str = "warn",
    fired_at: datetime = _NOW,
    escalated_at: datetime | None = None,
) -> AlertFiredResponse:
    return AlertFiredResponse.model_validate(
        dict(
            fired_id=uuid.uuid4(),
            rule_id=_RULE_ID,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key="abc123",
            severity=severity,
            current_value=75.0,
            triggered_reason="test",
            fired_at=fired_at,
            escalated_at=escalated_at,
            last_seen_at=fired_at,
        )
    )


def _make_engine(
    last_fired: AlertFiredResponse | None = None,
    upsert_return: tuple[AlertFiredResponse, bool] | None = None,
) -> AlertEngine:
    rule_repo = MagicMock()
    fired_repo = MagicMock()
    fired_repo.get_last_fired = AsyncMock(return_value=last_fired)
    if upsert_return is None:
        upsert_return = (_fired(), True)
    fired_repo.upsert_or_update_last_seen = AsyncMock(return_value=upsert_return)
    return AlertEngine(rule_repo=rule_repo, fired_repo=fired_repo)


class TestEvaluateMetricOk:
    async def test_ok_below_warn(self) -> None:
        engine = _make_engine()
        result = await engine.evaluate_metric(rule=_rule(), current_value=50.0, now=_NOW)
        assert result.fired is False
        assert result.severity == "ok"
        assert result.throttled is False

    async def test_ok_operator_lt(self) -> None:
        rule = _rule(threshold_warn=30.0, threshold_critical=10.0, operator="<")
        engine = _make_engine()
        result = await engine.evaluate_metric(rule=rule, current_value=50.0, now=_NOW)
        assert result.severity == "ok"
        assert result.fired is False


class TestEvaluateMetricWarn:
    async def test_warn_first_time(self) -> None:
        engine = _make_engine(last_fired=None, upsert_return=(_fired("warn"), True))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="key-abc"):
            result = await engine.evaluate_metric(rule=_rule(), current_value=75.0, now=_NOW)
        assert result.fired is True
        assert result.severity == "warn"
        assert result.throttled is False
        assert result.dedup_existed is False

    async def test_critical_first_time(self) -> None:
        engine = _make_engine(last_fired=None, upsert_return=(_fired("critical"), True))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="key-crit"):
            result = await engine.evaluate_metric(rule=_rule(), current_value=95.0, now=_NOW)
        assert result.fired is True
        assert result.severity == "critical"


class TestEvaluateMetricThrottled:
    async def test_throttled_within_window(self) -> None:
        recent = _fired(fired_at=_NOW - timedelta(seconds=60))
        engine = _make_engine(last_fired=recent)
        result = await engine.evaluate_metric(
            rule=_rule(throttle_seconds=300), current_value=75.0, now=_NOW
        )
        assert result.throttled is True
        assert result.fired is False
        assert result.severity == "warn"

    async def test_not_throttled_after_window(self) -> None:
        old = _fired(fired_at=_NOW - timedelta(seconds=400))
        engine = _make_engine(last_fired=old, upsert_return=(_fired("warn"), True))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="key-ok"):
            result = await engine.evaluate_metric(
                rule=_rule(throttle_seconds=300), current_value=75.0, now=_NOW
            )
        assert result.throttled is False
        assert result.fired is True


class TestEvaluateMetricDedup:
    async def test_dedup_existed(self) -> None:
        engine = _make_engine(last_fired=None, upsert_return=(_fired("warn"), False))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="key-dup"):
            result = await engine.evaluate_metric(rule=_rule(), current_value=75.0, now=_NOW)
        assert result.dedup_existed is True
        assert result.fired is False
        assert result.severity == "warn"


class TestSingleThreshold:
    async def test_critical_only_rule(self) -> None:
        rule = _rule(threshold_warn=None, threshold_critical=90.0)
        engine = _make_engine(last_fired=None, upsert_return=(_fired("critical"), True))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="k"):
            result = await engine.evaluate_metric(rule=rule, current_value=95.0, now=_NOW)
        assert result.fired is True
        assert result.severity == "critical"

    async def test_warn_only_rule(self) -> None:
        rule = _rule(threshold_warn=70.0, threshold_critical=None)
        engine = _make_engine(last_fired=None, upsert_return=(_fired("warn"), True))
        with patch("aegis.server.engines.alert_engine.compute_dedup_key", return_value="k"):
            result = await engine.evaluate_metric(rule=rule, current_value=75.0, now=_NOW)
        assert result.fired is True
        assert result.severity == "warn"


class TestCheckEscalationNeeded:
    def _engine(self) -> AlertEngine:
        return AlertEngine(rule_repo=MagicMock(), fired_repo=MagicMock())

    async def test_escalation_needed_after_delay(self) -> None:
        engine = self._engine()
        fired = _fired(
            severity="warn",
            fired_at=_NOW - timedelta(seconds=2000),
        )
        rule = _rule(escalation_delay_seconds=1800)
        assert await engine.check_escalation_needed(fired=fired, rule=rule, now=_NOW) is True

    async def test_escalation_not_needed_too_soon(self) -> None:
        engine = self._engine()
        fired = _fired(severity="warn", fired_at=_NOW - timedelta(seconds=600))
        rule = _rule(escalation_delay_seconds=1800)
        assert await engine.check_escalation_needed(fired=fired, rule=rule, now=_NOW) is False

    async def test_escalation_not_needed_critical(self) -> None:
        engine = self._engine()
        fired = _fired(severity="critical", fired_at=_NOW - timedelta(seconds=3600))
        rule = _rule(escalation_delay_seconds=1800)
        assert await engine.check_escalation_needed(fired=fired, rule=rule, now=_NOW) is False

    async def test_escalation_not_needed_already_escalated(self) -> None:
        engine = self._engine()
        fired = _fired(
            severity="warn",
            fired_at=_NOW - timedelta(seconds=3600),
            escalated_at=_NOW - timedelta(seconds=1000),
        )
        rule = _rule(escalation_delay_seconds=1800)
        assert await engine.check_escalation_needed(fired=fired, rule=rule, now=_NOW) is False
