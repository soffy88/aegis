"""Orchestration cron scheduler — P0-3 refactored with LoopSupervisor.

Runs background loops as independent supervised tasks:
- Event correlator:   every 5 min
- Capacity check:     every 60 min
- Alert escalation:   every 2 min
- Metrics scrape:     every 15 s (per-target interval gates actual scrapes)
- Anomaly scan:       every 60 s (EWMA)
- Webhook delivery:   every 5 s (drains the delivery queue)
- Recording:          every 30 s (derive rate gauges, e.g. container_cpu_percent)
- Uptime probe:       every 20 s (HTTP probes; per-target interval gates)
- Autoheal policies:  every 30 s (policy-driven; cooldown + dry_run gate actions)
- Alert evaluation:   every 30 s (threshold rules vs fresh metrics)
- Stale task reaper:  every 5 min
- Retention:          every 60 min (prune + storage guard)
- Rollup:             every 60 min (downsample to hourly)
- Deadman:            every 60 s (internal deadman + external heartbeat)
- Self-backup:        every 60 min (check if due)
- Drift scan:         every 10 min
- DDNS refresh:       every 5 min

All loops supervised with persistence, auto-restart, and health observability.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime
from typing import Any

from aegis.server.orchestration.loop_supervisor import register_loop

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


def _jittered(interval: float) -> float:
    return interval * random.uniform(0.9, 1.1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# In-memory last seen for deadman evaluation (updated by each loop on tick)
_LOOP_LAST_SEEN: dict[str, datetime] = {}

# Supervised loops with their expected intervals (for deadman)
_SUPERVISED_LOOPS: dict[str, float] = {}


def _tick(name: str, interval: float) -> None:
    """Mark loop as alive this tick — called by each loop after successful iteration."""
    _LOOP_LAST_SEEN[name] = _utcnow()
    # Also update the supervised loop state
    from aegis.server.orchestration.loop_supervisor import get_loop_states  # noqa: PLC0415

    states = get_loop_states()
    if name in states:
        states[name].last_tick_at = _utcnow()


async def _correlator_loop() -> None:
    from aegis.server.orchestration.event_correlator import (
        run_correlator_for_all_orgs,  # noqa: PLC0415
    )
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(20, 40))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_correlator_for_all_orgs(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("correlator_cron_error err=%s", exc)
        _tick("correlator", _CORRELATOR_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_CORRELATOR_INTERVAL_SEC))


async def _capacity_loop(alerter: Any | None) -> None:
    from aegis.server.api.routers.metrics import prune_old_metrics  # noqa: PLC0415
    from aegis.server.orchestration.capacity import run_capacity_check  # noqa: PLC0415
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    await asyncio.sleep(random.uniform(30, 60))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_capacity_check(conn=conn, alerter=alerter)
                await prune_old_metrics(conn, get_settings().agent_metrics_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("capacity_cron_error err=%s", exc)
        _tick("capacity", _CAPACITY_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_CAPACITY_INTERVAL_SEC))


def _build_webhook_dispatcher(conn: Any) -> Any:
    from aegis.server.engines.webhook_dispatcher import WebhookDispatcher  # noqa: PLC0415
    from aegis.server.repositories.webhook_delivery_repository import (  # noqa: PLC0415
        WebhookDeliveryQueueRepository,
    )
    from aegis.server.repositories.webhook_subscription_repository import (  # noqa: PLC0415
        WebhookSubscriptionRepository,
    )

    return WebhookDispatcher(
        sub_repo=WebhookSubscriptionRepository(conn),
        delivery_repo=WebhookDeliveryQueueRepository(conn),
    )


async def _escalation_loop() -> None:
    from aegis.server.orchestration.alert_escalation import (
        run_alert_escalation,  # noqa: PLC0415
    )
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(25, 50))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_alert_escalation(
                    conn=conn,
                    webhook_dispatcher=_build_webhook_dispatcher(conn),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("escalation_cron_error err=%s", exc)
        _tick("escalation", _ESCALATION_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_ESCALATION_INTERVAL_SEC))


async def _scrape_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.metrics_scraper import scrape_due_targets  # noqa: PLC0415

    await asyncio.sleep(random.uniform(5, 15))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await scrape_due_targets(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("scrape_cron_error err=%s", exc)
        _tick("scrape", _SCRAPE_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_SCRAPE_INTERVAL_SEC))


async def _autoheal_policy_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.autoheal_policy import run_autoheal_policies  # noqa: PLC0415

    await asyncio.sleep(random.uniform(25, 45))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_autoheal_policies(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("autoheal_policy_cron_error err=%s", exc)
        _tick("autoheal", _AUTOHEAL_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_AUTOHEAL_INTERVAL_SEC))


async def _stale_task_reaper_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.stale_task_reaper import run_stale_task_reaper  # noqa: PLC0415

    await asyncio.sleep(random.uniform(30, 60))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_stale_task_reaper(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stale_task_reaper_cron_error err=%s", exc)
        _tick("reaper", _REAPER_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_REAPER_INTERVAL_SEC))


async def _uptime_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.uptime_prober import probe_due_targets  # noqa: PLC0415

    await asyncio.sleep(random.uniform(10, 25))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await probe_due_targets(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("uptime_cron_error err=%s", exc)
        _tick("uptime", _UPTIME_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_UPTIME_INTERVAL_SEC))


async def _recording_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.metric_recording import (
        record_container_cpu_percent,  # noqa: PLC0415
        record_host_cpu_percent,  # noqa: PLC0415
        record_host_memory,  # noqa: PLC0415
    )

    await asyncio.sleep(random.uniform(20, 35))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await record_container_cpu_percent(conn)
                await record_host_cpu_percent(conn)
                await record_host_memory(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("recording_cron_error err=%s", exc)
        _tick("recording", _RECORDING_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_RECORDING_INTERVAL_SEC))


async def _alert_eval_loop() -> None:
    from aegis.server.orchestration.alert_evaluation import (
        run_alert_evaluation,  # noqa: PLC0415
    )
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(15, 30))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_alert_evaluation(
                    conn=conn,
                    webhook_dispatcher=_build_webhook_dispatcher(conn),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("alert_eval_cron_error err=%s", exc)
        _tick("alert_eval", _ALERT_EVAL_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_ALERT_EVAL_INTERVAL_SEC))


async def _delivery_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(3, 10))
    while True:
        try:
            async with get_pool().acquire() as conn:
                dispatcher = _build_webhook_dispatcher(conn)
                for _ in range(_DELIVERY_DRAIN_BATCHES):
                    stats = await dispatcher.deliver_batch()
                    if not any(stats.values()):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("delivery_cron_error err=%s", exc)
        _tick("delivery", _DELIVERY_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_DELIVERY_INTERVAL_SEC))


async def _anomaly_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.anomaly_scan import scan_anomalies  # noqa: PLC0415

    await asyncio.sleep(random.uniform(40, 70))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await scan_anomalies(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("anomaly_cron_error err=%s", exc)
        _tick("anomaly", _ANOMALY_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_ANOMALY_INTERVAL_SEC))


# Retention
_PRUNE_BATCH_ROWS = 50_000
_PRUNE_MAX_BATCHES_PER_TABLE = 200


async def _prune_table(*, table: str, ts_column: str, retain_days: float) -> int:
    for ident in (table, ts_column):
        if not ident.replace("_", "").isalnum():
            raise ValueError(f"unsafe identifier in retention registry: {ident!r}")

    from aegis.server.persistence import get_pool  # noqa: PLC0415

    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("  # noqa: S608
        f" SELECT ctid FROM {table} WHERE {ts_column} < now() - ($1 || ' days')::interval"
        f" LIMIT {_PRUNE_BATCH_ROWS})"
    )
    total = 0
    for _ in range(_PRUNE_MAX_BATCHES_PER_TABLE):
        async with get_pool().acquire() as conn:
            status = await conn.execute(sql, str(retain_days))
        deleted = int(status.rsplit(" ", 1)[-1]) if status.startswith("DELETE") else 0
        total += deleted
        if deleted < _PRUNE_BATCH_ROWS:
            break
        await asyncio.sleep(0.1)
    return total


async def _retention_loop() -> None:
    from oprim import disk_usage  # noqa: PLC0415

    from aegis.server.persistence.retention import (  # noqa: PLC0415
        RETENTION,
        STORAGE_GUARD_PERCENT,
    )
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    await asyncio.sleep(random.uniform(60, 120))
    while True:
        cfg = get_settings()
        for entry in RETENTION:
            try:
                deleted = await _prune_table(
                    table=str(entry["table"]),
                    ts_column=str(entry["ts_column"]),
                    retain_days=float(entry["retain_days"]),  # type: ignore[arg-type]
                )
                if deleted:
                    log.info(
                        "retention_pruned table=%s rows=%d retain_days=%s",
                        entry["table"],
                        deleted,
                        entry["retain_days"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("retention_prune_error table=%s err=%s", entry["table"], exc)
        try:
            du = await asyncio.to_thread(
                disk_usage,
                path=cfg.platform_alerter_disk_path,
                threshold_percent=STORAGE_GUARD_PERCENT,
            )
            if getattr(du, "over_threshold", False):
                log.warning(
                    "storage_guard_breach path=%s used=%.1f%% threshold=%.0f%%",
                    cfg.platform_alerter_disk_path,
                    getattr(du, "used_percent", 0.0),
                    STORAGE_GUARD_PERCENT,
                )
                try:
                    from aegis.server.services.disk_reclaim import reclaim_disk  # noqa: PLC0415

                    rc = await asyncio.to_thread(reclaim_disk, cfg)
                    log.warning(
                        "disk_reclaim targets=%d freed=%dB touched=%d dry_run=%s",
                        rc["targets"],
                        rc["freed_bytes"],
                        rc["touched"],
                        rc["dry_run"],
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("disk_reclaim_error err=%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("storage_guard_error err=%s", exc)
        _tick("retention", _RETENTION_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_RETENTION_INTERVAL_SEC))


async def _rollup_loop() -> None:
    from datetime import timedelta  # noqa: PLC0415

    from oprim import metric_downsample_rollup  # noqa: PLC0415

    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    await asyncio.sleep(random.uniform(90, 150))
    while True:
        cfg = get_settings()
        try:
            since = _utcnow() - timedelta(hours=_ROLLUP_LOOKBACK_HOURS)
            res = await asyncio.to_thread(
                metric_downsample_rollup,
                dsn=cfg.postgres_dsn,
                source_table="agent_metrics",
                dest_table="agent_metrics_rollup_1h",
                ts_column="ts",
                value_column="value",
                agg="avg",
                bucket_seconds=3600,
                since=since,
                label_columns=["metric_name", "hostname"],
            )
            if getattr(res, "rows_written", 0):
                log.info("metric_rollup rows_written=%d", res.rows_written)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("metric_rollup_error err=%s", exc)
        _tick("rollup", _ROLLUP_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_ROLLUP_INTERVAL_SEC))


async def _drift_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from aegis.server.services.compose_drift import scan_drift  # noqa: PLC0415

    await asyncio.sleep(random.uniform(60, 120))
    while True:
        cfg = get_settings()
        if cfg.compose_drift_enabled:
            try:
                async with get_pool().acquire() as conn:
                    await scan_drift(conn, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("compose_drift_error err=%s", exc)
        _tick("drift", _DRIFT_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_DRIFT_INTERVAL_SEC))


async def _ddns_refresh_loop() -> None:
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services import ddns as ddns_svc  # noqa: PLC0415

    await asyncio.sleep(random.uniform(30, 60))
    while True:
        try:
            async with get_pool().acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, org_id FROM ddns_configs WHERE enabled = TRUE LIMIT 1000"
                )
                if len(rows) >= 1000:
                    log.warning("ddns_refresh: >= 1000 enabled records — result truncated")
                for r in rows:
                    try:
                        await ddns_svc.update_now(conn, org_id=r["org_id"], config_id=r["id"])
                    except ddns_svc.DdnsPrimitiveUnavailable:
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ddns_refresh_error id=%s err=%s", r["id"], exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("ddns_refresh_loop_error err=%s", exc)
        _tick("ddns_refresh", _DDNS_REFRESH_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_DDNS_REFRESH_INTERVAL_SEC))


async def _deadman_loop() -> None:
    from oprim import heartbeat_emit  # noqa: PLC0415
    from oskill.deadman_evaluate import deadman_evaluate  # noqa: PLC0415

    from aegis.server.orchestration.loop_supervisor import any_required_dead  # noqa: PLC0415
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    await asyncio.sleep(random.uniform(45, 75))
    while True:
        now = _utcnow()
        cfg = get_settings()

        # Check for stalled loops (internal deadman)
        stalled: list[str] = []
        for name, interval in _SUPERVISED_LOOPS.items():
            try:
                verdict = deadman_evaluate(
                    subject=name,
                    last_seen=_LOOP_LAST_SEEN.get(name),
                    expected_interval_seconds=float(interval),
                    now=now,
                    grace_seconds=float(interval) * _DEADMAN_GRACE_FACTOR
                    + _DEADMAN_STARTUP_GRACE_SEC,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("deadman_eval_error loop=%s err=%s", name, exc)
                continue
            if verdict.silent and verdict.ever_seen:
                stalled.append(f"{name}(overdue={verdict.overdue_seconds:.0f}s)")

        # P0-3: Also check supervisor state for crashed/dead required loops
        dead_required: list[str] = []
        if any_required_dead():
            from aegis.server.orchestration.loop_supervisor import (
                get_dead_required_loops,  # noqa: PLC0415
            )

            dead_required = get_dead_required_loops()

        all_stalled = stalled + dead_required

        if all_stalled:
            log.error(
                "loop_deadman_stalled loops=%s (编排循环停摆,MAPE-K 断链)", ", ".join(all_stalled)
            )

        url = cfg.deadman_heartbeat_url
        if url:
            if all_stalled:
                log.warning(
                    "deadman_heartbeat_suppressed reason=loops_stalled → 外部死人开关将触发"
                )
            else:
                try:
                    res = await asyncio.to_thread(
                        heartbeat_emit, url=url, timeout_sec=cfg.deadman_heartbeat_timeout_sec
                    )
                    if not getattr(res, "delivered", False):
                        log.warning(
                            "deadman_heartbeat_undelivered status=%s err=%s",
                            getattr(res, "status_code", None),
                            getattr(res, "error", None),
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("deadman_heartbeat_error err=%s", exc)
        _tick("deadman", _HEARTBEAT_INTERVAL_SEC)
        await asyncio.sleep(_jittered(_HEARTBEAT_INTERVAL_SEC))


_last_self_backup: datetime | None = None


async def _self_backup_loop() -> None:
    global _last_self_backup
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from aegis.server.services.self_backup import (  # noqa: PLC0415
        prune_self_backups,
        run_self_backup,
    )

    await asyncio.sleep(random.uniform(90, 150))
    while True:
        cfg = get_settings()
        interval = float(cfg.self_backup_interval_hours) * 3600.0
        now = _utcnow()
        due = _last_self_backup is None or (now - _last_self_backup).total_seconds() >= interval
        if interval > 0 and due:
            try:
                result = await asyncio.to_thread(run_self_backup, cfg)
                _last_self_backup = now
                if result.get("status") == "completed":
                    f = result.get("findings")
                    log.info(
                        "self_backup_ok id=%s size=%s sha256=%s",
                        getattr(f, "backup_id", "?"),
                        getattr(f, "size_bytes", "?"),
                        (getattr(f, "checksum_sha256", "") or "")[:12],
                    )
                else:
                    log.error(
                        "self_backup_failed err=%s (控制面 DB 未产出可恢复工件)",
                        result.get("error"),
                    )
                await asyncio.to_thread(prune_self_backups, cfg, int(cfg.self_backup_retain))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("self_backup_error err=%s", exc)
        _tick("self_backup", _SELF_BACKUP_TICK_SEC)
        await asyncio.sleep(_jittered(_SELF_BACKUP_TICK_SEC))


# Register all loops with the supervisor (P0-3)
# Format: (name, interval, factory, required)
# required=True → external heartbeat stops if this loop dies

register_loop("correlator", _CORRELATOR_INTERVAL_SEC, _correlator_loop, required=True)
register_loop("capacity", _CAPACITY_INTERVAL_SEC, lambda: _capacity_loop(None), required=True)
register_loop("escalation", _ESCALATION_INTERVAL_SEC, _escalation_loop, required=True)
register_loop("scrape", _SCRAPE_INTERVAL_SEC, _scrape_loop, required=True)
register_loop("anomaly", _ANOMALY_INTERVAL_SEC, _anomaly_loop, required=True)
register_loop("delivery", _DELIVERY_INTERVAL_SEC, _delivery_loop, required=True)
register_loop("recording", _RECORDING_INTERVAL_SEC, _recording_loop, required=True)
register_loop("uptime", _UPTIME_INTERVAL_SEC, _uptime_loop, required=True)
register_loop("autoheal", _AUTOHEAL_INTERVAL_SEC, _autoheal_policy_loop, required=True)
register_loop("reaper", _REAPER_INTERVAL_SEC, _stale_task_reaper_loop, required=True)
register_loop("alert_eval", _ALERT_EVAL_INTERVAL_SEC, _alert_eval_loop, required=True)
register_loop("retention", _RETENTION_INTERVAL_SEC, _retention_loop, required=True)
register_loop("rollup", _ROLLUP_INTERVAL_SEC, _rollup_loop, required=True)
register_loop("drift", _DRIFT_INTERVAL_SEC, _drift_loop, required=False)
register_loop("ddns_refresh", _DDNS_REFRESH_INTERVAL_SEC, _ddns_refresh_loop, required=False)
register_loop("self_backup", _SELF_BACKUP_TICK_SEC, _self_backup_loop, required=False)

# Populate _SUPERVISED_LOOPS for deadman (includes deadman itself as a supervised loop)
for name, interval in [
    ("correlator", _CORRELATOR_INTERVAL_SEC),
    ("capacity", _CAPACITY_INTERVAL_SEC),
    ("escalation", _ESCALATION_INTERVAL_SEC),
    ("scrape", _SCRAPE_INTERVAL_SEC),
    ("anomaly", _ANOMALY_INTERVAL_SEC),
    ("delivery", _DELIVERY_INTERVAL_SEC),
    ("recording", _RECORDING_INTERVAL_SEC),
    ("uptime", _UPTIME_INTERVAL_SEC),
    ("autoheal", _AUTOHEAL_INTERVAL_SEC),
    ("reaper", _REAPER_INTERVAL_SEC),
    ("alert_eval", _ALERT_EVAL_INTERVAL_SEC),
    ("retention", _RETENTION_INTERVAL_SEC),
    ("rollup", _ROLLUP_INTERVAL_SEC),
    ("drift", _DRIFT_INTERVAL_SEC),
    ("ddns_refresh", _DDNS_REFRESH_INTERVAL_SEC),
    ("self_backup", _SELF_BACKUP_TICK_SEC),
    ("deadman", _HEARTBEAT_INTERVAL_SEC),
]:
    _SUPERVISED_LOOPS[name] = interval


_LOOP_RUNNER_ROLE = "aegis.loop_runner"


async def _acquire_loop_runner_role() -> Any | None:
    from oprim import pg_advisory_lock_plan  # noqa: PLC0415

    from aegis.server.persistence import get_pool  # noqa: PLC0415

    plan = pg_advisory_lock_plan(name=_LOOP_RUNNER_ROLE)
    try:
        pool = get_pool()
        conn = await pool.acquire()
    except Exception as exc:  # noqa: BLE001
        log.warning("loop_runner_pool_error err=%s (loops disabled)", exc)
        return None
    try:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", plan.key)
    except Exception as exc:  # noqa: BLE001
        log.warning("loop_runner_lock_error err=%s", exc)
        await pool.release(conn)
        return None
    if got:
        return conn
    await pool.release(conn)
    return None


async def _cron_main(alerter: Any | None) -> None:
    runner_conn = await _acquire_loop_runner_role()
    if runner_conn is None:
        log.info("loop_runner_role_not_acquired instance=API-only (另一实例持锁)")
        return
    log.info("loop_runner_role_acquired starting supervised orchestration loops")

    # Start all supervised loops
    from aegis.server.orchestration.loop_supervisor import start_supervised_loops  # noqa: PLC0415

    tasks = await start_supervised_loops()

    try:
        # Wait for all tasks (they run forever until cancelled)
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    finally:
        from aegis.server.orchestration.loop_supervisor import (
            shutdown_supervised_loops,  # noqa: PLC0415
        )

        await shutdown_supervised_loops()

        from oprim import pg_advisory_lock_plan  # noqa: PLC0415

        from aegis.server.persistence import get_pool  # noqa: PLC0415

        with contextlib.suppress(Exception):
            await runner_conn.fetchval(
                "SELECT pg_advisory_unlock($1)",
                pg_advisory_lock_plan(name=_LOOP_RUNNER_ROLE).key,
            )
        with contextlib.suppress(Exception):
            await get_pool().release(runner_conn)


def start_orchestration_crons(alerter: Any | None = None) -> asyncio.Task[Any]:
    """Start orchestration crons as a single background task (supervised)."""
    task = asyncio.ensure_future(_cron_main(alerter))
    log.info(
        "orchestration_crons_started correlator=%ds capacity=%ds escalation=%ds",
        _CORRELATOR_INTERVAL_SEC,
        _CAPACITY_INTERVAL_SEC,
        _ESCALATION_INTERVAL_SEC,
    )
    return task
