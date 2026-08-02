"""Orchestration cron scheduler.

Runs background loops:
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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_CORRELATOR_INTERVAL_SEC = 300  # 5 min
_CAPACITY_INTERVAL_SEC = 3600  # 60 min
_ESCALATION_INTERVAL_SEC = 120  # 2 min
_SCRAPE_INTERVAL_SEC = 15  # tick; each target's own interval gates actual scrapes
_ANOMALY_INTERVAL_SEC = 60  # EWMA anomaly scan
_DELIVERY_INTERVAL_SEC = 5  # tick; drains the webhook delivery queue (next_attempt_at gates)
_DELIVERY_DRAIN_BATCHES = 20  # max batches per tick so one org's backlog can't wedge the loop
_ALERT_EVAL_INTERVAL_SEC = 30  # evaluate threshold rules against fresh metrics
_RECORDING_INTERVAL_SEC = 30  # derive rate gauges (e.g. container_cpu_percent)
_UPTIME_INTERVAL_SEC = 20  # tick; each target's own interval gates actual probes
_AUTOHEAL_INTERVAL_SEC = 30  # evaluate autoheal policies (cooldown gates real actions)
_REAPER_INTERVAL_SEC = 300  # 5 min: reap stuck "processing" tasks per declared policies
_RETENTION_INTERVAL_SEC = 3600  # 60 min: prune expired telemetry (§7) + storage guard
_ROLLUP_INTERVAL_SEC = 3600  # 60 min: downsample raw metrics into hourly rollups (§4.2)
_ROLLUP_LOOKBACK_HOURS = 3  # re-aggregate last N hours each run (idempotent upsert 兜迟到点)
_HEARTBEAT_INTERVAL_SEC = 60  # emit external dead-man heartbeat (§6 L1)
_DRIFT_INTERVAL_SEC = 600  # 10 min: config-as-code drift scan (§10/§3.7)
_DDNS_REFRESH_INTERVAL_SEC = 300  # 5 min: refresh enabled DDNS records (CasaOS parity)
_SELF_BACKUP_TICK_SEC = 3600  # 每小时醒来判断是否到自备份周期 (§11.4)
_DEADMAN_GRACE_FACTOR = 3.0  # loop silent > interval×3 (+startup grace) ⇒ stalled
_DEADMAN_STARTUP_GRACE_SEC = 180.0  # 不误报 boot 期尚未首轮 tick 的循环


def _jittered(interval: float) -> float:
    """±10% jitter so multiple replicas don't synchronize onto the DB."""
    return interval * random.uniform(0.9, 1.1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# §4.2/§6: 各编排循环每轮 tick 更新存活时刻(self-metrics 时间戳);_deadman_loop 据此评估卡死。
_LOOP_LAST_SEEN: dict[str, datetime] = {}

# 受死人监督的循环 → 其标称间隔(秒)。key 即 _tick(name) 的 name。
_SUPERVISED_LOOPS: dict[str, float] = {
    "correlator": _CORRELATOR_INTERVAL_SEC,
    "capacity": _CAPACITY_INTERVAL_SEC,
    "escalation": _ESCALATION_INTERVAL_SEC,
    "scrape": _SCRAPE_INTERVAL_SEC,
    "anomaly": _ANOMALY_INTERVAL_SEC,
    "delivery": _DELIVERY_INTERVAL_SEC,
    "recording": _RECORDING_INTERVAL_SEC,
    "uptime": _UPTIME_INTERVAL_SEC,
    "autoheal": _AUTOHEAL_INTERVAL_SEC,
    "reaper": _REAPER_INTERVAL_SEC,
    "alert_eval": _ALERT_EVAL_INTERVAL_SEC,
    "retention": _RETENTION_INTERVAL_SEC,
    "rollup": _ROLLUP_INTERVAL_SEC,
    "ddns_refresh": _DDNS_REFRESH_INTERVAL_SEC,
}


async def _tick(name: str, interval: float) -> None:
    """标记 name 循环本轮存活 + 抖动睡眠。取代裸 sleep(_jittered(...))。"""
    _LOOP_LAST_SEEN[name] = _utcnow()
    await asyncio.sleep(_jittered(interval))


async def _correlator_loop() -> None:
    from aegis.server.orchestration.event_correlator import (
        run_correlator_for_all_orgs,  # noqa: PLC0415
    )
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    # Small staggered initial delay (not a full interval) so the first run
    # happens soon after boot but replicas don't all fire at once.
    await asyncio.sleep(random.uniform(20, 40))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_correlator_for_all_orgs(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("correlator_cron_error err=%s", exc)
        await _tick("correlator", _CORRELATOR_INTERVAL_SEC)


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
                # Retention: prune stale agent_metrics (hourly is fine for a daily TTL).
                await prune_old_metrics(conn, get_settings().agent_metrics_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("capacity_cron_error err=%s", exc)
        await _tick("capacity", _CAPACITY_INTERVAL_SEC)


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
        await _tick("escalation", _ESCALATION_INTERVAL_SEC)


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
        await _tick("scrape", _SCRAPE_INTERVAL_SEC)


async def _autoheal_policy_loop() -> None:
    """Evaluate policy-driven closed-loop autoheal. Per-policy cooldown + dry_run
    default mean real container restarts only happen for explicitly-enabled policies."""
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
        await _tick("autoheal", _AUTOHEAL_INTERVAL_SEC)


async def _stale_task_reaper_loop() -> None:
    """Reap stuck 'processing' tasks per declared stale_task_policies (devplatform
    Phase 1). dry_run default + per-policy max cap mean real writes only happen for
    explicitly-enabled, non-dry-run policies."""
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
        await _tick("reaper", _REAPER_INTERVAL_SEC)


async def _uptime_loop() -> None:
    """Probe HTTP uptime targets (~20s tick; per-target interval gates) and record
    probe_up/probe_latency_ms so rules can alert on services going down."""
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
        await _tick("uptime", _UPTIME_INTERVAL_SEC)


async def _recording_loop() -> None:
    """Derive gauges from scraped counters/gauges so threshold rules & the overview
    tiles have them: per-container container_cpu_percent, plus whole-host
    node_cpu_percent and node_memory_used_bytes/percent. Runs behind the scrape."""
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
        await _tick("recording", _RECORDING_INTERVAL_SEC)


async def _alert_eval_loop() -> None:
    """Evaluate enabled threshold rules against fresh metrics every ~30s.

    Without this loop, AlertEngine.evaluate_metric had no periodic caller and
    user-configured rules never auto-fired. Shares the webhook dispatcher so a
    newly-fired alert enqueues its `alert.fired` notification.
    """
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
        await _tick("alert_eval", _ALERT_EVAL_INTERVAL_SEC)


async def _delivery_loop() -> None:
    """Drain the webhook delivery queue.

    `enqueue_event` (escalation loop, alert engine, error alerter, envelope) only
    *queues* deliveries; without this loop nothing is ever sent. Each tick claims
    due rows (`next_attempt_at <= now`, FOR UPDATE SKIP LOCKED) and POSTs them,
    looping until the queue drains or the per-tick batch cap is hit so backoff and
    retry/dead-letter (already implemented in WebhookDispatcher) actually fire.
    """
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(3, 10))
    while True:
        try:
            async with get_pool().acquire() as conn:
                dispatcher = _build_webhook_dispatcher(conn)
                for _ in range(_DELIVERY_DRAIN_BATCHES):
                    stats = await dispatcher.deliver_batch()
                    if not any(stats.values()):
                        break  # queue empty (or nothing due) — wait for next tick
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("delivery_cron_error err=%s", exc)
        await _tick("delivery", _DELIVERY_INTERVAL_SEC)


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
        await _tick("anomaly", _ANOMALY_INTERVAL_SEC)


# Retention deletes run in bounded batches: the tables this prunes are the biggest
# in the system, and one unbounded DELETE would hold a multi-GB transaction, bloat
# WAL and stall every writer. Each batch commits on its own.
_PRUNE_BATCH_ROWS = 50_000
_PRUNE_MAX_BATCHES_PER_TABLE = 200  # ≤10M rows per table per tick, then wait for the next


async def _prune_table(*, table: str, ts_column: str, retain_days: float) -> int:
    """Delete rows older than *retain_days* from *table*, in committed batches.

    Table/column names come from the in-repo RETENTION registry (never user input),
    but they are still validated as plain identifiers so this can never become an
    injection sink if that registry is ever fed from config.
    """
    for ident in (table, ts_column):
        if not ident.replace("_", "").isalnum():
            raise ValueError(f"unsafe identifier in retention registry: {ident!r}")

    from aegis.server.persistence import get_pool  # noqa: PLC0415

    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("  # noqa: S608 — identifiers validated above
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
        await asyncio.sleep(0.1)  # breathe: never monopolize the pool
    return total


async def _retention_loop() -> None:
    """§7/I6: 按 retention 登记表分批删除过期遥测(有界写入者)+ 存储守卫(§7 70% 大声告警).

    删除走本进程已有的 asyncpg 连接池(`_prune_table`),不再依赖 oprim.retention_prune 的
    psycopg 驱动 —— 生产实测该驱动未随镜像安装,导致每个表每轮都 `retention_prune_error`,
    保留策略事实上从未生效(agent_metrics 攒到 31 天 / 1.5 亿行 / 64GB 把生产盘撑到 100%,
    正是 §7 这个循环该防住的故障)。disk_usage 仍是 sync 原语,走 to_thread。
    单条 prune 失败不阻断其它条目。"""
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
                    "storage_guard_breach path=%s used=%.1f%% threshold=%.0f%% "
                    "(retention/rollup 可能未收口;生产盘将被平台遥测拖垮)",
                    cfg.platform_alerter_disk_path,
                    getattr(du, "used_percent", 0.0),
                    STORAGE_GUARD_PERCENT,
                )
                # §5.2 R2 磁盘回收:在 data_dir 自有子树内回收可再生文件(allowlist 硬护栏);
                # R2 破坏性 → 默认 dry_run 只统计,运维显式关闭 disk_cleanup_dry_run 才真删。
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
        await _tick("retention", _RETENTION_INTERVAL_SEC)


async def _rollup_loop() -> None:
    """§4.2/§7: 把 agent_metrics 原始点按小时桶降采样 upsert 进 rollup 表(幂等),使长期趋势
    有界(原始点 15d 保留,rollup 90d)。每轮重聚合最近 N 小时兜迟到点;metric_downsample_rollup
    是 sync psycopg → to_thread。"""
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
        await _tick("rollup", _ROLLUP_INTERVAL_SEC)


async def _drift_loop() -> None:
    """§10/§3.7: 周期比对声明态(installed_apps.image)与运行态(容器镜像),漂移写 config.drift
    一等 change 事件。docker 不可达/禁用则空转。"""
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
        await asyncio.sleep(_jittered(_DRIFT_INTERVAL_SEC))


async def _ddns_refresh_loop() -> None:
    """CasaOS parity: periodically push the current IP to every enabled DDNS config.

    update_now delegates to oprim.ddns_update (an HTTPS call); one config's failure
    (bad creds / provider down) never blocks the others. Degrades quietly when the
    oprim pin lacks ddns_update.
    """
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services import ddns as ddns_svc  # noqa: PLC0415

    await asyncio.sleep(random.uniform(30, 60))
    while True:
        try:
            async with get_pool().acquire() as conn:
                rows = await conn.fetch("SELECT id, org_id FROM ddns_configs WHERE enabled = TRUE")
                for r in rows:
                    try:
                        await ddns_svc.update_now(conn, org_id=r["org_id"], config_id=r["id"])
                    except ddns_svc.DdnsPrimitiveUnavailable:
                        break  # pin not bumped — skip the whole round
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ddns_refresh_error id=%s err=%s", r["id"], exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("ddns_refresh_loop_error err=%s", exc)
        await _tick("ddns_refresh", _DDNS_REFRESH_INTERVAL_SEC)


async def _deadman_loop() -> None:
    """§6 死人开关:内部循环存活评估(deadman_evaluate) + L1 外部心跳(heartbeat_emit).

    - 内部:对每个受监督循环,若曾见但现静默超 interval×factor+startup_grace ⇒ 卡死,大声 error。
    - 外部(L1):仅当所有循环健康时才向 cfg.deadman_heartbeat_url 发心跳;任一卡死则**抑制**心跳
      → 外部 watcher 超时告警("谁看门人":aegis 自身失能由平台外部发现,不自证清白)。
    URL 空 = 外部死人禁用(degraded,仅内部 error 日志)。heartbeat_emit 是 sync → to_thread。"""
    from oprim import heartbeat_emit  # noqa: PLC0415
    from oskill.deadman_evaluate import deadman_evaluate  # noqa: PLC0415

    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    await asyncio.sleep(random.uniform(45, 75))  # 让各循环有时间首轮 tick
    while True:
        now = _utcnow()
        cfg = get_settings()
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
            # ever_seen 且 silent = 真卡死(曾运行后停摆);never_seen 由 startup_grace 兜住不误报
            if verdict.silent and verdict.ever_seen:
                stalled.append(f"{name}(overdue={verdict.overdue_seconds:.0f}s)")
        if stalled:
            log.error(
                "loop_deadman_stalled loops=%s (编排循环停摆,MAPE-K 断链)", ", ".join(stalled)
            )

        url = cfg.deadman_heartbeat_url
        if url:
            if stalled:
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
        await asyncio.sleep(_jittered(_HEARTBEAT_INTERVAL_SEC))


_last_self_backup: datetime | None = None


async def _self_backup_loop() -> None:
    """§11.4: 定时 pg_dump 平台自身控制面 DB(可恢复是底线)。每小时醒来,到周期才真备份。

    run_self_backup/prune 是 sync(pg_dump/文件)→ to_thread。status=failed(如 pg_dump 缺失)
    大声 error 但不崩循环。仅 loop-runner 实例跑(_cron_main 已由 advisory 锁把关)。"""
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
        await asyncio.sleep(_jittered(_SELF_BACKUP_TICK_SEC))


_LOOP_RUNNER_ROLE = "aegis.loop_runner"


async def _acquire_loop_runner_role() -> Any | None:
    """尝试成为 loop-runner —— 在专用长连接上取 PG advisory 角色锁 (DESIGN §4.1 / C-4.1).

    用机制取缔"单 worker"纪律:多实例只有拿到锁的那个跑编排循环,其余只跑 API。锁随连接
    存活(session 级),连接持有到进程退出,断开时 PG 自动释放。key 由 oprim.pg_advisory_lock_plan
    从角色名稳定派生;SQL 用 aegis 的 asyncpg 占位符($1)。

    Returns 持有的连接(赢得角色)或 None(未拿到 → 本实例只跑 API)。
    """
    from oprim import pg_advisory_lock_plan  # noqa: PLC0415

    from aegis.server.persistence import get_pool  # noqa: PLC0415

    plan = pg_advisory_lock_plan(name=_LOOP_RUNNER_ROLE)
    try:
        pool = get_pool()
        conn = await pool.acquire()  # 专用连接,持有到进程退出(不归还池)
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
    # §4.1: 只有拿到 loop-runner 角色锁的实例才跑编排循环(结构性取缔单 worker;多 worker 安全)。
    runner_conn = await _acquire_loop_runner_role()
    if runner_conn is None:
        log.info("loop_runner_role_not_acquired instance=API-only (另一实例持锁)")
        return
    log.info("loop_runner_role_acquired starting orchestration loops")
    try:
        await asyncio.gather(
            _correlator_loop(),
            _capacity_loop(alerter),
            _escalation_loop(),
            _scrape_loop(),
            _anomaly_loop(),
            _delivery_loop(),
            _recording_loop(),
            _uptime_loop(),
            _autoheal_policy_loop(),
            _stale_task_reaper_loop(),
            _alert_eval_loop(),
            _retention_loop(),
            _rollup_loop(),
            _deadman_loop(),
            _self_backup_loop(),
            _drift_loop(),
            _ddns_refresh_loop(),
            return_exceptions=True,
        )
    finally:
        from oprim import pg_advisory_lock_plan  # noqa: PLC0415

        from aegis.server.persistence import get_pool  # noqa: PLC0415

        with contextlib.suppress(Exception):
            await runner_conn.fetchval(
                "SELECT pg_advisory_unlock($1)",
                pg_advisory_lock_plan(name=_LOOP_RUNNER_ROLE).key,
            )
        with contextlib.suppress(Exception):
            await get_pool().release(runner_conn)


def start_orchestration_crons(alerter: Any | None = None) -> asyncio.Task:
    """Start both cron loops as a single background task."""
    task = asyncio.ensure_future(_cron_main(alerter))
    log.info(
        "orchestration_crons_started correlator=%ds capacity=%ds escalation=%ds",
        _CORRELATOR_INTERVAL_SEC,
        _CAPACITY_INTERVAL_SEC,
        _ESCALATION_INTERVAL_SEC,
    )
    return task
