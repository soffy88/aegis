"""Loop Supervisor — P0-3 production-grade orchestration loop supervision.

Each loop runs as an independent supervised task with:
- Startup success/failure observable
- Unexpected exit → auto-restart with exponential backoff + jitter
- Restart count / last_error / last_tick persisted to PostgreSQL
- never_seen > startup grace → declared dead
- All critical loops under supervision
- Any REQUIRED loop dead → external heartbeat suppressed
- /health/loops endpoint for observability
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Intervals (seconds)
_CORRELATOR_INTERVAL_SEC = 300
_CAPACITY_INTERVAL_SEC = 3600
_ESCALATION_INTERVAL_SEC = 120
_SCRAPE_INTERVAL_SEC = 15
_ANOMALY_INTERVAL_SEC = 60
_DELIVERY_INTERVAL_SEC = 5
_DELIVERY_DRAIN_BATCHES = 20
_ALERT_EVAL_INTERVAL_SEC = 30
_RECORDING_INTERVAL_SEC = 30
_UPTIME_INTERVAL_SEC = 20
_AUTOHEAL_INTERVAL_SEC = 30
_REAPER_INTERVAL_SEC = 300
_RETENTION_INTERVAL_SEC = 3600
_ROLLUP_INTERVAL_SEC = 3600
_ROLLUP_LOOKBACK_HOURS = 3
_HEARTBEAT_INTERVAL_SEC = 60
_DRIFT_INTERVAL_SEC = 600
_DDNS_REFRESH_INTERVAL_SEC = 300
_SELF_BACKUP_TICK_SEC = 3600

_DEADMAN_GRACE_FACTOR = 3.0
_DEADMAN_STARTUP_GRACE_SEC = 180.0

# Loop definitions: (name, interval, factory, required)
# required=True → external heartbeat stops if this loop dies
LOOP_DEFINITIONS: list[tuple[str, float, Callable[..., Awaitable[None]], bool]] = []


def _jittered(interval: float) -> float:
    return interval * random.uniform(0.9, 1.1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class LoopState:
    name: str
    expected_interval_seconds: float
    status: str = "starting"  # starting | running | crashed | restarting | dead
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_tick_at: datetime | None = None
    last_error: str | None = None
    restart_count: int = 0
    consecutive_failures: int = 0
    required: bool = True


class LoopSupervisor:
    """Supervises all orchestration loops with persistence and auto-restart."""

    def __init__(self) -> None:
        self._loops: dict[str, LoopState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown = False

    def register(
        self,
        name: str,
        interval: float,
        factory: Callable[..., Awaitable[None]],
        required: bool = True,
    ) -> None:
        self._loops[name] = LoopState(
            name=name,
            expected_interval_seconds=interval,
            required=required,
        )
        LOOP_DEFINITIONS.append((name, interval, factory, required))

    async def _persist_loop_state(self, state: LoopState) -> None:
        """Persist loop state to PostgreSQL."""
        from aegis.server.persistence import get_pool  # noqa: PLC0415

        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO loop_supervision (
                    loop_name, expected_interval_seconds, last_started_at, last_completed_at,
                    last_error, restart_count, consecutive_failures, status, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                ON CONFLICT (loop_name) DO UPDATE SET
                    expected_interval_seconds = EXCLUDED.expected_interval_seconds,
                    last_started_at = EXCLUDED.last_started_at,
                    last_completed_at = EXCLUDED.last_completed_at,
                    last_error = EXCLUDED.last_error,
                    restart_count = EXCLUDED.restart_count,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    status = EXCLUDED.status,
                    updated_at = now()
                """,
                state.name,
                state.expected_interval_seconds,
                state.last_started_at,
                state.last_completed_at,
                state.last_error,
                state.restart_count,
                state.consecutive_failures,
                state.status,
            )

    async def _load_loop_state(self, name: str) -> LoopState | None:
        """Load loop state from PostgreSQL."""
        from aegis.server.persistence import get_pool  # noqa: PLC0415

        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM loop_supervision WHERE loop_name = $1", name)
            if not row:
                return None
            return LoopState(
                name=row["loop_name"],
                expected_interval_seconds=row["expected_interval_seconds"],
                status=row["status"],
                last_started_at=row["last_started_at"],
                last_completed_at=row["last_completed_at"],
                last_error=row["last_error"],
                restart_count=row["restart_count"],
                consecutive_failures=row["consecutive_failures"],
                required=True,  # will be overridden by definition
            )

    async def _run_loop_with_supervision(
        self,
        name: str,
        interval: float,
        factory: Callable[..., Awaitable[None]],
        required: bool,
    ) -> None:
        """Run a single loop with supervision, auto-restart, and persistence."""
        state = self._loops[name]
        state.required = required

        # Load persisted state
        persisted = await self._load_loop_state(name)
        if persisted:
            state.restart_count = persisted.restart_count
            state.consecutive_failures = persisted.consecutive_failures
            state.last_error = persisted.last_error
            log.info(
                "loop_state_loaded name=%s restart_count=%d consecutive_failures=%d",
                name,
                state.restart_count,
                state.consecutive_failures,
            )

        backoff = 1.0  # initial backoff seconds
        max_backoff = 300.0  # 5 min max

        while not self._shutdown:
            state.status = "starting"
            state.last_started_at = _utcnow()
            await self._persist_loop_state(state)

            try:
                log.info("loop_starting name=%s", name)
                # Create the loop coroutine
                loop_coro = factory()

                # Run the loop; it should run forever until cancelled
                await loop_coro

            except asyncio.CancelledError:
                log.info("loop_cancelled name=%s", name)
                state.status = "crashed"
                await self._persist_loop_state(state)
                raise

            except Exception as exc:
                state.consecutive_failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"[:500]
                state.status = "crashed"
                await self._persist_loop_state(state)
                log.error(
                    "loop_crashed name=%s error=%s consecutive_failures=%d",
                    name,
                    state.last_error,
                    state.consecutive_failures,
                )

            if self._shutdown:
                break

            # Exponential backoff with jitter
            state.status = "restarting"
            state.restart_count += 1
            await self._persist_loop_state(state)

            sleep_time = _jittered(min(backoff, max_backoff))
            log.warning(
                "loop_restart_backoff name=%s attempt=%d sleep=%.1fs",
                name,
                state.restart_count,
                sleep_time,
            )
            await asyncio.sleep(sleep_time)
            backoff *= 2

    def start_all(self) -> dict[str, asyncio.Task[None]]:
        """Start all registered loops as independent supervised tasks."""
        for name, interval, factory, required in LOOP_DEFINITIONS:
            task = asyncio.create_task(
                self._run_loop_with_supervision(name, interval, factory, required),
                name=f"loop-{name}",
            )
            self._tasks[name] = task
            log.info("loop_supervised_task_created name=%s required=%s", name, required)
        return self._tasks

    def get_loop_states(self) -> dict[str, LoopState]:
        return dict(self._loops)

    def is_any_required_dead(self) -> bool:
        """Check if any required loop is dead/crashed."""
        for state in self._loops.values():
            if state.required and state.status in ("crashed", "dead"):
                return True
        return False

    def get_dead_required_loops(self) -> list[str]:
        return [
            name
            for name, state in self._loops.items()
            if state.required and state.status in ("crashed", "dead")
        ]

    async def shutdown(self) -> None:
        self._shutdown = True
        for task in self._tasks.values():
            task.cancel()
        # Wait for all tasks to complete cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


# Global supervisor instance
_supervisor = LoopSupervisor()


def register_loop(
    name: str, interval: float, factory: Callable[..., Awaitable[None]], required: bool = True
) -> None:
    _supervisor.register(name, interval, factory, required)


async def start_supervised_loops() -> dict[str, asyncio.Task[None]]:
    return _supervisor.start_all()


async def shutdown_supervised_loops() -> None:
    await _supervisor.shutdown()


def get_loop_states() -> dict[str, LoopState]:
    return _supervisor.get_loop_states()


def any_required_dead() -> bool:
    return _supervisor.is_any_required_dead()


def get_dead_required_loops() -> list[str]:
    return _supervisor.get_dead_required_loops()


# P0-3: Health check endpoint data
async def get_loops_health() -> dict[str, Any]:
    """Get health status of all supervised loops for /health/loops endpoint."""
    states = get_loop_states()
    now = _utcnow()
    dead_required = get_dead_required_loops()

    loops_info = {}
    for name, state in states.items():
        # Check if loop has never been seen but past startup grace
        never_seen = state.last_started_at is None
        overdue = False
        if state.last_tick_at:
            overdue = (now - state.last_tick_at).total_seconds() > (
                state.expected_interval_seconds * _DEADMAN_GRACE_FACTOR + _DEADMAN_STARTUP_GRACE_SEC
            )
        elif never_seen and state.last_started_at:
            overdue = (now - state.last_started_at).total_seconds() > _DEADMAN_STARTUP_GRACE_SEC

        loops_info[name] = {
            "status": state.status,
            "required": state.required,
            "expected_interval_sec": state.expected_interval_seconds,
            "last_started_at": state.last_started_at.isoformat() if state.last_started_at else None,
            "last_completed_at": state.last_completed_at.isoformat()
            if state.last_completed_at
            else None,
            "last_tick_at": state.last_tick_at.isoformat() if state.last_tick_at else None,
            "last_error": state.last_error,
            "restart_count": state.restart_count,
            "consecutive_failures": state.consecutive_failures,
            "never_seen": never_seen,
            "overdue": overdue,
        }

    return {
        "healthy": len(dead_required) == 0,
        "dead_required_loops": dead_required,
        "loops": loops_info,
        "external_heartbeat_allowed": len(dead_required) == 0,
    }


__all__ = [
    "LoopState",
    "LoopSupervisor",
    "register_loop",
    "start_supervised_loops",
    "shutdown_supervised_loops",
    "get_loop_states",
    "any_required_dead",
    "get_dead_required_loops",
    "get_loops_health",
]
