"""Alert Engine — 服务层引擎 (v1.0 §6.5).

evaluate_metric 流程:
1. oprim.evaluate_threshold_rule 双阈值评估 (两阈值都有时)
2. oprim.should_throttle 节流判定
3. oprim.compute_dedup_key 生成去重 key
4. AlertFiredRepository.upsert_or_update_last_seen (同桶只存首次)

不做: 通知发送 (C2-5), rule CRUD (router), YAML 配置 (M2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from oprim import compute_dedup_key, evaluate_threshold_rule, should_throttle
from pydantic import BaseModel

from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
from aegis.server.schemas.alerting import AlertFiredResponse, AlertRuleResponse

_THRESHOLD_OPS = {">=", ">", "<", "<="}


def _eval_single_threshold(
    current_value: float,
    threshold: float,
    operator: str,
    severity: Literal["warn", "critical"],
) -> tuple[bool, str]:
    """Simple comparison for rules with only one threshold set.

    oprim.evaluate_threshold_rule requires both warn + critical, so for
    single-threshold rules the engine does a direct comparison here.
    '==' operator is also handled here since oprim doesn't support it.
    """
    ops = {
        ">=": current_value >= threshold,
        ">": current_value > threshold,
        "<=": current_value <= threshold,
        "<": current_value < threshold,
        "==": current_value == threshold,
    }
    triggered = ops.get(operator, False)
    reason = f"{current_value} {operator} {threshold} → {'triggered' if triggered else 'ok'}"
    return triggered, reason


class AlertEvaluationResult(BaseModel):
    rule_id: UUID
    fired: bool
    throttled: bool
    dedup_existed: bool
    severity: Literal["ok", "warn", "critical"]
    fired_row: AlertFiredResponse | None
    reason: str


class AlertEngine:
    def __init__(
        self,
        *,
        rule_repo: AlertRuleRepository,
        fired_repo: AlertFiredRepository,
    ) -> None:
        self.rule_repo = rule_repo
        self.fired_repo = fired_repo

    async def evaluate_metric(
        self,
        *,
        rule: AlertRuleResponse,
        current_value: float,
        now: datetime | None = None,
    ) -> AlertEvaluationResult:
        """Single metric evaluation against a rule.

        Args:
            rule: AlertRuleResponse from DB.
            current_value: Observed metric value.
            now: Injected for testing; defaults to datetime.now(UTC).

        Returns:
            AlertEvaluationResult with fired/throttled/dedup_existed/severity.
        """
        now = now or datetime.now(UTC)

        # Step 1: threshold evaluation
        severity, triggered, reason = self._evaluate_thresholds(rule, current_value)

        if not triggered:
            return AlertEvaluationResult(
                rule_id=rule.rule_id,
                fired=False,
                throttled=False,
                dedup_existed=False,
                severity="ok",
                fired_row=None,
                reason=reason,
            )

        # Step 2: throttle check (oprim)
        last_fired = await self.fired_repo.get_last_fired(rule_id=rule.rule_id)
        last_fired_at = last_fired.fired_at if last_fired else None

        if should_throttle(
            last_fired_at=last_fired_at,
            throttle_seconds=rule.throttle_seconds,
            now=now,
        ):
            return AlertEvaluationResult(
                rule_id=rule.rule_id,
                fired=False,
                throttled=True,
                dedup_existed=False,
                severity=severity,
                fired_row=last_fired,
                reason=f"throttled (last_fired={last_fired_at}, throttle={rule.throttle_seconds}s)",
            )

        # Step 3: dedup key (oprim)
        dedup_key = compute_dedup_key(
            rule_id=str(rule.rule_id),
            entity_id=f"{rule.project_id}:{rule.metric}",
            bucket_seconds=rule.dedup_bucket_seconds,
            bucket_anchor=now,
        )

        # Step 4: upsert into alert_fired_history
        fired_row, is_new = await self.fired_repo.upsert_or_update_last_seen(
            rule_id=rule.rule_id,
            org_id=rule.org_id,
            project_id=rule.project_id,
            dedup_key=dedup_key,
            severity=severity,
            current_value=current_value,
            triggered_reason=reason,
            now=now,
        )

        return AlertEvaluationResult(
            rule_id=rule.rule_id,
            fired=is_new,
            throttled=False,
            dedup_existed=not is_new,
            severity=severity,
            fired_row=fired_row,
            reason=reason if is_new else f"dedup_existed (key={dedup_key[:16]}...)",
        )

    def _evaluate_thresholds(
        self,
        rule: AlertRuleResponse,
        current_value: float,
    ) -> tuple[Literal["ok", "warn", "critical"], bool, str]:
        """Return (severity, triggered, reason).

        Uses oprim.evaluate_threshold_rule when both thresholds are set.
        Falls back to direct comparison for single-threshold rules or '==' operator.
        """
        warn = rule.threshold_warn
        critical = rule.threshold_critical
        op = rule.operator

        # Both thresholds set + operator supported by oprim → use oprim
        if warn is not None and critical is not None and op in _THRESHOLD_OPS:
            result = evaluate_threshold_rule(
                current_value=current_value,
                rule_spec={
                    "metric": rule.metric,
                    "threshold": {"warn": warn, "critical": critical},
                    "operator": op,
                },
            )
            sev: Literal["ok", "warn", "critical"] = result.severity
            return sev, result.triggered, result.reason

        # Single-threshold or '==' fallback
        if critical is not None:
            triggered, reason = _eval_single_threshold(current_value, critical, op, "critical")
            return ("critical" if triggered else "ok"), triggered, reason

        # warn-only
        triggered, reason = _eval_single_threshold(current_value, warn, op, "warn")  # type: ignore[arg-type]
        return ("warn" if triggered else "ok"), triggered, reason

    async def check_escalation_needed(
        self,
        *,
        fired: AlertFiredResponse,
        rule: AlertRuleResponse,
        now: datetime | None = None,
    ) -> bool:
        """Return True if warn alert should escalate to critical.

        Conditions: severity=warn + escalated_at is None + elapsed >= escalation_delay_seconds.
        Caller (scheduler) should then: INSERT new critical row + mark_escalated().
        """
        if fired.severity != "warn":
            return False
        if fired.escalated_at is not None:
            return False
        now = now or datetime.now(UTC)
        elapsed = (now - fired.fired_at).total_seconds()
        return elapsed >= rule.escalation_delay_seconds
