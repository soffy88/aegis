"""AutoHeal Engine — 状态机骨架 + 调 oskill (Step 15 §2.1 §8.6).

硬约束:
- ❌ Engine 内部不写"重启 + 等健康"业务逻辑 — 那是 oskill.restart_and_verify
- ✅ Engine = 状态机 + 调 plugin / oskill
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

import asyncpg
from oskill import RestartAndVerifyOutcome, restart_and_verify

from aegis.server.persistence import append_event

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AutoHealDispatcher (legacy skeleton, kept for alert routing)
# ---------------------------------------------------------------------------


class AutoHealDispatcher:
    """Match alerts to plugins and (optionally) dispatch them.

    Skeleton: loads plugins at init time, matches by name, logs the would-be
    invocation, and writes event_trail markers.
    """

    def __init__(self, plugins: dict[str, type[Any]], dry_run: bool = True) -> None:
        self._plugins = plugins
        self._dry_run = dry_run
        log.info("autoheal_dispatcher_ready plugins=%d dry_run=%s", len(plugins), dry_run)

    def find_matching_plugins(self, alert_name: str) -> list[type[Any]]:
        """Find plugins whose matches_alert pattern matches alert_name."""
        matches = []
        for cls in self._plugins.values():
            pattern = getattr(cls, "matches_alert", "")
            if pattern and pattern in alert_name:
                matches.append(cls)
        return matches

    async def dispatch(
        self,
        *,
        conn: asyncpg.Connection,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        alert_payload: dict[str, Any],
        trace_id: str,
        parent_event_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Dispatch matching plugins for the alert."""
        alert_name = alert_payload.get("alert_name", "")
        matched = self.find_matching_plugins(alert_name)

        if not matched:
            return {"matched": [], "outcome": "no_match"}

        results: list[dict[str, Any]] = []
        for cls in matched:
            event_id = await append_event(
                conn=conn,
                org_id=org_id,
                project_id=project_id,
                event_type="autoheal_triggered",
                severity="warning",
                autoheal_plugin=cls.name,
                payload={
                    "matches_alert": cls.matches_alert,
                    "dry_run": self._dry_run,
                    "alert_payload": alert_payload,
                },
                trace_id=trace_id,
                parent_id=parent_event_id,
                initiated_by="agent",
            )

            if self._dry_run:
                log.info("autoheal_dry_run plugin=%s alert=%s", cls.name, alert_name)
                results.append(
                    {"plugin": cls.name, "outcome": "dry_run", "event_id": str(event_id)}
                )
            else:
                log.warning("autoheal_execute_not_implemented plugin=%s", cls.name)
                results.append({"plugin": cls.name, "outcome": "execute_stub"})

        return {"matched": [c.name for c in matched], "results": results}


class EngineState(StrEnum):
    pending = "pending"
    pre_check = "pre_check"
    awaiting_approval = "awaiting_approval"  # C2-3: gate created, waiting for human decision
    executing = "executing"
    post_verify = "post_verify"
    rolling_back = "rolling_back"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"  # C2-3: approval rejected or expired
    escalated_to_human = "escalated_to_human"


@dataclass
class PluginResult:
    success: bool = True
    requires_restart_verify: bool = False
    container_id: str = ""
    health_check_url: str | None = None


class AutoHealPlugin(Protocol):
    """Plugin protocol — plugins implement these methods."""

    name: str

    async def pre_check(self, context: dict[str, Any]) -> PluginResult: ...

    # C2-3: optional — plugins that don't define this are treated as requires_approval=False.
    # Use getattr(plugin, 'requires_approval', None) at call sites; Protocol default
    # implementations are NOT inherited by duck-typed implementors.
    async def requires_approval(self, context: dict[str, Any]) -> bool:
        return False

    async def execute(self, context: dict[str, Any]) -> PluginResult: ...
    async def rollback(self, context: dict[str, Any]) -> PluginResult: ...


@dataclass
class AutoHealEngine:
    """State machine engine that orchestrates plugin lifecycle + oskill calls."""

    state: EngineState = EngineState.pending
    outcome: RestartAndVerifyOutcome | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    # C2-3: injected to enable the approval gate workflow.
    # None = approval feature disabled (existing behaviour preserved).
    release_gate_service: Any | None = None
    # C2-5: injected to enable webhook notifications on terminal states.
    # None = webhook feature disabled (existing behaviour preserved).
    webhook_dispatcher: Any | None = None

    async def handle(
        self,
        *,
        alert: dict[str, Any],
        action_plan: dict[str, Any],
        plugin: AutoHealPlugin | None,
        # C2-3 optional context — required only when approval may be needed
        org_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
    ) -> EngineState:
        """Run the autoheal lifecycle for a given alert + action plan.

        Returns final state.
        """
        if plugin is None:
            self.state = EngineState.escalated_to_human
            self._log("no_plugin_matched", alert=alert)
            return self.state

        context = {"alert": alert, "action_plan": action_plan}

        # 1. pre_check
        self.state = EngineState.pre_check
        self._log("pre_check_start", plugin=plugin.name)
        pre = await plugin.pre_check(context)
        if not pre.success:
            self.state = EngineState.failed
            self._log("pre_check_failed", plugin=plugin.name)
            await self._maybe_enqueue_webhook(
                org_id=org_id, plugin_name=plugin.name, event_id=event_id
            )
            return self.state

        # 2. C2-3: approval gate (skipped if service not injected or context incomplete)
        approval_fn = getattr(plugin, "requires_approval", None)
        if (
            callable(approval_fn)
            and self.release_gate_service is not None
            and org_id is not None
            and project_id is not None
            and await approval_fn(context)
        ):
            action_kind = getattr(plugin, "action_kind", plugin.name)
            gate = await self.release_gate_service.create_gate(
                org_id=org_id,
                project_id=project_id,
                requested_by=requested_by or uuid.uuid4(),
                action_kind=action_kind,
                action_payload=action_plan,
                autoheal_event_id=event_id,
                expires_in_hours=24,
            )
            self.state = EngineState.awaiting_approval
            self._log("approval_gate_created", gate_id=str(gate.gate_id), plugin=plugin.name)

            await self._wait_for_decision(gate_id=gate.gate_id, org_id=org_id)

            decided = await self.release_gate_service.repo.get(
                gate_id=gate.gate_id, org_id=org_id, lazy_expire=False
            )
            decision_state = decided.state if decided else "unknown"
            if decision_state != "approved":
                self.state = EngineState.cancelled
                self._log("approval_cancelled", decision=decision_state, plugin=plugin.name)
                await self._maybe_enqueue_webhook(
                    org_id=org_id, plugin_name=plugin.name, event_id=event_id
                )
                return self.state

            self._log("approval_granted", plugin=plugin.name)

        # 3. execute (unchanged)
        self.state = EngineState.executing
        self._log("execute_start", plugin=plugin.name)
        exec_result = await plugin.execute(context)

        # 4. post_verify via oskill.restart_and_verify (unchanged)
        if exec_result.requires_restart_verify:
            self.state = EngineState.post_verify
            self._log("post_verify_start", container_id=exec_result.container_id)
            self.outcome = restart_and_verify(
                container_id=exec_result.container_id,
                health_check_url=exec_result.health_check_url,
                timeout_sec=60,
            )
            if self.outcome.verified_healthy:
                self.state = EngineState.completed
                self._log("completed", plugin=plugin.name)
            else:
                # rollback
                self.state = EngineState.rolling_back
                self._log("rollback_start", plugin=plugin.name)
                await plugin.rollback(context)
                self.state = EngineState.failed
                self._log("rollback_done", plugin=plugin.name)
        else:
            self.state = EngineState.completed
            self._log("completed_no_verify", plugin=plugin.name)

        await self._maybe_enqueue_webhook(org_id=org_id, plugin_name=plugin.name, event_id=event_id)
        return self.state

    async def _maybe_enqueue_webhook(
        self,
        *,
        org_id: uuid.UUID | None,
        plugin_name: str,
        event_id: uuid.UUID | None,
    ) -> None:
        """Enqueue autoheal.* webhook if dispatcher injected and state is terminal."""
        if self.webhook_dispatcher is None or org_id is None:
            return
        terminal_map = {
            EngineState.completed: "autoheal.completed",
            EngineState.failed: "autoheal.failed",
            EngineState.cancelled: "autoheal.cancelled",
        }
        event_type = terminal_map.get(self.state)
        if event_type is None:
            return
        await self.webhook_dispatcher.enqueue_event(
            org_id=org_id,
            event_type=event_type,
            payload={
                "event_id": str(event_id) if event_id else None,
                "plugin": plugin_name,
                "final_state": self.state,
            },
        )

    async def _wait_for_decision(
        self,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        poll_interval_sec: int = 10,
        max_wait_sec: int = 24 * 3600,
    ) -> None:
        """Poll release_gate until state != pending or max_wait_sec exceeded.

        Uses asyncio.wait_for to cap total blocking time and ensure the task
        is properly cancelled when the engine is torn down, preventing DB
        connection leaks in long-running approval waits.
        """

        async def _poll() -> None:
            elapsed = 0
            while elapsed < max_wait_sec:
                gate = await self.release_gate_service.repo.get(
                    gate_id=gate_id, org_id=org_id, lazy_expire=True
                )
                if not gate or gate.state != "pending":
                    return
                await asyncio.sleep(poll_interval_sec)
                elapsed += poll_interval_sec

        try:
            await asyncio.wait_for(_poll(), timeout=float(max_wait_sec))
        except TimeoutError:
            log.warning(
                "autoheal_wait_decision_timeout gate_id=%s org_id=%s max_wait_sec=%d",
                gate_id,
                org_id,
                max_wait_sec,
            )

    def _log(self, event: str, **kwargs: Any) -> None:
        self.history.append({"event": event, "state": self.state, **kwargs})
        log.info("autoheal_%s state=%s %s", event, self.state, kwargs)
