"""AutoHeal Engine — 4-phase lifecycle state machine.

Aegis-native engine (oservice has no autoheal engine in v0.4.x).
Integrates with C2-3 AutoHeal Approval (release_gate_service).

Lifecycle: pending → diagnosing → [awaiting_approval →] applying → verifying
           → succeeded
           → rolling_back → rolled_back
           → failed (any phase)

oskill usage:
  diagnosing:  oskill.diagnose_pattern_match (signal classification)
  applying:    circuit_breaker_check guard before plugin execution (S3)
  verifying:   oskill.verify_health_after_action
  rolling_back: omodul.rollback_app (BACKLOG-076)

Design basis: AEGIS_DESIGN v1.1.0 §8.6 + C2-3 approval gate (release_gate_service).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from aegis_autoheal_sdk import (
    ActionResultStatus,
    AutoHealContext,
    ServiceInfo,
    Severity,
)
from omodul.rollback_app import RollbackAppConfig, RollbackAppInput, rollback_app
from oprim import (
    db_insert,
    docker_container_restart,
    http_request_once,
)
from oskill import circuit_breaker_check, diagnose_pattern_match

from aegis.server.plugins.registry import get_plugin_callable

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
class AegisServiceInfo(ServiceInfo):
    _name: str
    _health: str
    _version: str | None

    @property
    def name(self) -> str:
        return self._name

    @property
    def health(self) -> str:
        return self._health

    @property
    def version(self) -> str | None:
        return self._version


class AegisAutoHealContext(AutoHealContext):
    def __init__(
        self,
        service: ServiceInfo,
        alert_payload: dict[str, Any],
        env: Severity,
        trace_id: str,
        docker_host: str = "unix:///var/run/docker.sock",
    ):
        self._service = service
        self._alert_payload = alert_payload
        self._env = env
        self._trace_id = trace_id
        self._docker_host = docker_host

    @property
    def service(self) -> ServiceInfo:
        return self._service

    @property
    def alert_payload(self) -> dict[str, Any]:
        return self._alert_payload

    @property
    def org_environment(self) -> Severity:
        return self._env

    @property
    def trace_id(self) -> str:
        return self._trace_id

    async def systemctl_restart(self, service: str) -> None:
        log.info("ctx_systemctl_restart service=%s", service)
        # Stub: systemd integration not in oprim yet

    async def kill_process(self, *, name: str | None = None, pid: int | None = None) -> None:
        log.info("ctx_kill_process name=%s pid=%s", name, pid)

    async def docker_restart(self, container: str) -> None:
        log.info("ctx_docker_restart container=%s", container)
        await asyncio.get_event_loop().run_in_executor(
            None, docker_container_restart, container_id=container, docker_host=self._docker_host
        )

    async def k8s_pod_delete(self, *, namespace: str, pod: str) -> None:
        log.info("ctx_k8s_pod_delete ns=%s pod=%s", namespace, pod)

    async def http_get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        log.info("ctx_http_get url=%s", url)
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: http_request_once(method="GET", url=url, **kwargs)
        )
        return {
            "status_code": resp.status_code,
            "body": resp.text,
            "elapsed_ms": (
                int(resp.elapsed.total_seconds() * 1000) if hasattr(resp, "elapsed") else 0
            ),
        }

    async def alert_human(self, message: str, *, channel: str = "slack") -> None:
        log.info("ctx_alert_human channel=%s msg=%s", channel, message)

    async def get_secret(self, path: str) -> str:
        return "stub-secret"

    async def emit_trail_event(
        self, *, event_type: str, severity: str = "info", payload: dict[str, Any] | None = None
    ) -> None:
        db_insert(
            table="aegis_event_trail",
            row={
                "trace_id": self._trace_id,
                "event_type": event_type,
                "severity": severity,
                "payload": payload or {},
                "created_at": "NOW()",
            },
        )


@dataclass
class AutoHealEngine:
    """State machine engine for AutoHeal lifecycle.

    Inject release_gate_service to enable C2-3 approval gate.
    Leave None to skip approval entirely (existing behaviour preserved).
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
        env: Severity = Severity.DEV,
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
        if circuit_breaker_thresholds:
            cb_result = circuit_breaker_check(
                samples=health_samples or [],
                thresholds=circuit_breaker_thresholds,
            )
            self._log("circuit_breaker_check", tripped=cb_result.should_trip)
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

        # ── Phase 2: applying ────────────────────────────────────────────────
        self.state = AutoHealState.applying
        self._log("applying_start", plugin=plugin_name)

        plugin_cls = get_plugin_callable(plugin_name)
        if plugin_cls is None:
            self.state = AutoHealState.failed
            self._log("plugin_not_found", plugin=plugin_name)
            return self.state

        # Prepare context
        trace_id = str(uuid.uuid4())
        service_info = AegisServiceInfo(
            _name=action_plan.get("instance_name", "unknown"),
            _health="unknown",
            _version=action_plan.get("rollback_to_version"),
        )
        ctx = AegisAutoHealContext(
            service=service_info,
            alert_payload=signal,
            env=env,
            trace_id=trace_id,
            docker_host=action_plan.get("docker_host", "unix:///var/run/docker.sock"),
        )

        plugin = plugin_cls()
        try:
            # Lifecycle: pre_check -> execute -> post_verify
            if not await plugin.pre_check(ctx):
                self._log("plugin_pre_check_skipped")
                self.state = AutoHealState.succeeded
                return self.state

            result = await plugin.execute(ctx)
            self._log("plugin_execute_done", status=result.status, detail=result.detail)

            if result.status == ActionResultStatus.OK:
                # ── Phase 3: verifying ───────────────────────────────────────
                self.state = AutoHealState.verifying
                self._log("verifying_start")
                if await plugin.post_verify(ctx):
                    self.state = AutoHealState.succeeded
                    self._log("succeeded")
                    return self.state
                else:
                    self._log("plugin_post_verify_failed")
            elif result.status == ActionResultStatus.ESCALATE:
                self.state = AutoHealState.failed
                self._log("plugin_escalated", to=result.escalate_to)
                return self.state

            # If execute failed or post_verify failed, trigger plugin-level rollback
            self._log("triggering_plugin_rollback")
            await plugin.rollback(ctx)

        except Exception as exc:
            self._log("plugin_lifecycle_error", error=str(exc))
            try:
                await plugin.rollback(ctx)
            except Exception as rb_exc:
                self._log("plugin_rollback_error", error=str(rb_exc))

        # ── Phase 4: rolling_back (Infra-level rollback) ─────────────────────
        self.state = AutoHealState.rolling_back
        self._log("rollback_start")

        app_slug = action_plan.get("app_slug")
        instance_name = action_plan.get("instance_name")
        rollback_to_version = action_plan.get("rollback_to_version")

        if app_slug and instance_name and rollback_to_version:
            try:
                config = RollbackAppConfig(
                    app_slug=app_slug,
                    instance_name=instance_name,
                    rollback_to_version=rollback_to_version,
                    restore_data=action_plan.get("restore_data", True),
                )
                input_data = RollbackAppInput(
                    docker_host=action_plan.get("docker_host", "unix:///var/run/docker.sock"),
                    backup_bucket=action_plan.get("backup_bucket"),
                    backup_key=action_plan.get("backup_key"),
                    target_volume=action_plan.get("target_volume"),
                )

                with tempfile.TemporaryDirectory() as tmp:
                    await asyncio.get_event_loop().run_in_executor(
                        None, rollback_app, config, input_data, Path(tmp)
                    )
                self._log("rollback_done")
            except Exception as exc:
                self._log("rollback_failed", error=str(exc))
        else:
            self._log("rollback_skipped", reason="missing rollback metadata")

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
