"""AutoHeal Engine — 4-phase lifecycle state machine.

Aegis-native engine (oservice has no autoheal engine in v0.4.x).
Integrates with C2-3 AutoHeal Approval (release_gate_service).

Lifecycle: pending → diagnosing → [awaiting_approval →] applying → verifying
           → succeeded
           → rolling_back → rolled_back
           → failed (any phase)

oskill usage:
  diagnosing:  oskill.diagnose_pattern_match (signal classification)
  applying:    circuit_breaker_check guard before plugin stub (S3)
  verifying:   oskill.verify_health_after_action
  rolling_back: stub — oskill.rollback_execution not in v3.14.0
                TODO(AEGIS-BACKLOG-076): wire when available

Design basis: AEGIS_DESIGN v1.1.0 §8.6 + C2-3 approval gate (release_gate_service).
Not an oservice engine — Aegis owns this state machine directly.

TODO(S3): replace _apply_stub with real plugin.execute() via aegis-plugins entry_points.
TODO(AEGIS-BACKLOG-076): wire oskill.rollback_execution when oskill v3.15+ ships it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from oskill import circuit_breaker_check, diagnose_pattern_match, verify_health_after_action

log = logging.getLogger(__name__)


class AutoHealState(StrEnum):
    pending = "pending"
    diagnosing = "diagnosing"
    awaiting_approval = "awaiting_approval"  # C2-3 gate
    applying = "applying"
    verifying = "verifying"
    succeeded = "succeeded"
    rolling_back = "rolling_back"
    rolled_back = "rolled_back"
    failed = "failed"


@dataclass
class AutoHealEngine:
    """State machine engine for AutoHeal lifecycle.

    Inject release_gate_service to enable C2-3 approval gate.
    Leave None to skip approval entirely (existing behaviour preserved).

    Usage::

        engine = AutoHealEngine(release_gate_service=rgs)
        final = await engine.run(
            signal={"alert_name": "disk_full", "severity": "critical"},
            action_plan={"action_kind": "clear_logs", "requires_approval": True},
            service_url="http://localhost:8080/health",
            health_retries=5,
            org_id=org_id, project_id=project_id,
        )
        assert final in (AutoHealState.succeeded, AutoHealState.rolled_back, AutoHealState.failed)
    """

    state: AutoHealState = AutoHealState.pending
    history: list[dict[str, Any]] = field(default_factory=list)
    release_gate_service: Any | None = None

    async def run(
        self,
        *,
        signal: dict[str, Any],
        action_plan: dict[str, Any],
        plugin_name: str = "",
        service_url: str = "",
        health_retries: int = 5,
        health_samples: list[dict[str, Any]] | None = None,
        circuit_breaker_thresholds: dict[str, float] | None = None,
        min_confidence: float = 0.5,
        org_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
    ) -> AutoHealState:
        """Run the AutoHeal 4-phase lifecycle. Returns terminal state."""

        # ── Phase 1: diagnosing ──────────────────────────────────────────────
        self.state = AutoHealState.diagnosing
        patterns = action_plan.get("patterns")
        match_result = diagnose_pattern_match(
            signal=signal,
            patterns=patterns,
            min_confidence=min_confidence,
        )
        self._log(
            "diagnose_result",
            matched=match_result.matched,
            pattern=match_result.pattern_name,
            confidence=match_result.confidence,
        )

        if not match_result.matched:
            self.state = AutoHealState.failed
            self._log("diagnose_no_match_failed")
            return self.state

        # ── Circuit breaker guard ────────────────────────────────────────────
        if health_samples:
            cb_result = circuit_breaker_check(
                samples=health_samples,
                thresholds=circuit_breaker_thresholds,
            )
            self._log(
                "circuit_breaker", should_trip=cb_result.should_trip, reasons=cb_result.reasons
            )
            if cb_result.should_trip:
                self.state = AutoHealState.failed
                self._log("circuit_breaker_tripped_failed")
                return self.state

        # ── C2-3: approval gate ──────────────────────────────────────────────
        if (
            self.release_gate_service is not None
            and org_id is not None
            and project_id is not None
            and action_plan.get("requires_approval")
        ):
            gate = await self.release_gate_service.create_gate(
                org_id=org_id,
                project_id=project_id,
                requested_by=requested_by or uuid.uuid4(),
                action_kind=action_plan.get("action_kind", plugin_name),
                action_payload=action_plan,
                autoheal_event_id=None,
                expires_in_hours=24,
            )
            self.state = AutoHealState.awaiting_approval
            self._log("approval_gate_created", gate_id=str(gate.gate_id))

            await self._wait_for_decision(gate.gate_id, org_id)
            decided = await self.release_gate_service.repo.get(
                gate_id=gate.gate_id, org_id=org_id, lazy_expire=False
            )
            decision_state = decided.state if decided else "unknown"
            if decision_state != "approved":
                self.state = AutoHealState.failed
                self._log("approval_rejected", decision=decision_state)
                return self.state
            self._log("approval_granted")

        # ── Phase 2: applying (plugin stub — S3 real execution) ──────────────
        self.state = AutoHealState.applying
        self._log("applying_start", plugin=plugin_name)
        # TODO(S3 aegis-plugins): call real plugin.execute(signal, action_plan)
        log.info(
            "autoheal_apply_stub plugin=%s action_kind=%s (S3 TBD)",
            plugin_name,
            action_plan.get("action_kind"),
        )

        # ── Phase 3: verifying ───────────────────────────────────────────────
        self.state = AutoHealState.verifying
        self._log("verifying_start", service_url=service_url)

        if service_url:
            healthy = verify_health_after_action(service_url=service_url, retries=health_retries)
        else:
            healthy = True
            self._log("verifying_skip_no_url")

        if healthy:
            self.state = AutoHealState.succeeded
            self._log("succeeded")
            return self.state

        # ── Phase 4: rolling_back ────────────────────────────────────────────
        self.state = AutoHealState.rolling_back
        self._log("rollback_start")
        # TODO(AEGIS-BACKLOG-076): wire oskill.rollback_execution when oskill v3.15+ ships
        log.warning(
            "autoheal_rollback_stub plugin=%s"
            " (AEGIS-BACKLOG-076 oskill.rollback_execution missing)",
            plugin_name,
        )
        self.state = AutoHealState.rolled_back
        self._log("rolled_back")
        return self.state

    def _log(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "state": self.state, **kwargs}
        self.history.append(entry)
        log.info("autoheal_engine %s state=%s %s", event, self.state, kwargs)

    async def _wait_for_decision(
        self,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        poll_interval_sec: int = 10,
        max_wait_sec: int = 24 * 3600,
    ) -> None:
        elapsed = 0
        while elapsed < max_wait_sec:
            gate = await self.release_gate_service.repo.get(
                gate_id=gate_id, org_id=org_id, lazy_expire=True
            )
            if not gate or gate.state != "pending":
                return
            await asyncio.sleep(poll_interval_sec)
            elapsed += poll_interval_sec
