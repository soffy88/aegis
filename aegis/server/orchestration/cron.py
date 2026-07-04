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
import logging
import random
from datetime import datetime, timezone
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
_RETENTION_INTERVAL_SEC = 3600  # 60 min: prune expired telemetry (§7) + storage guard
_HEARTBEAT_INTERVAL_SEC = 60  # emit external dead-man heartbeat (§6 L1)
_SELF_BACKUP_TICK_SEC = 3600  # 每小时醒来判断是否到自备份周期 (§11.4)
_DEADMAN_GRACE_FACTOR = 3.0  # loop silent > interval×3 (+startup grace) ⇒ stalled
_DEADMAN_STARTUP_GRACE_SEC = 180.0  # 不误报 boot 期尚未首轮 tick 的循环


def _jittered(interval: float) -> float:
    """±10% jitter so multiple replicas don't synchronize onto the DB."""
    return interval * random.uniform(0.9, 1.1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    "alert_eval": _ALERT_EVAL_INTERVAL_SEC,
    "retention": _RETENTION_INTERVAL_SEC,
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
    """Derive rate-based gauges (container_cpu_percent) from scraped counters so
    threshold rules can alert on CPU. Runs just behind the scrape cadence."""
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.metric_recording import (
        record_container_cpu_percent,  # noqa: PLC0415
        record_node_percentages,  # noqa: PLC0415
    )

    await asyncio.sleep(random.uniform(20, 35))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await record_container_cpu_percent(conn)
                await record_node_percentages(conn)
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


async def _retention_loop() -> None:
    """§7/I6: 按 retention 登记表分批删除过期遥测(有界写入者)+ 存储守卫(§7 70% 大声告警).

    retention_prune/disk_usage 是 sync oprim 原语(psycopg/os.statvfs)→ 走 to_thread 不阻塞事件循环。
    单条 prune 失败不阻断其它条目;缺 psycopg 等致命错整体降级但不崩循环。"""
    from aegis.server.persistence.retention import (  # noqa: PLC0415
        RETENTION,
        STORAGE_GUARD_PERCENT,
    )
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from oprim import disk_usage, retention_prune  # noqa: PLC0415

    await asyncio.sleep(random.uniform(60, 120))
    while True:
        cfg = get_settings()
        for entry in RETENTION:
            try:
                res = await asyncio.to_thread(
                    retention_prune,
                    dsn=cfg.postgres_dsn,
                    table=str(entry["table"]),
                    ts_column=str(entry["ts_column"]),
                    retain_days=float(entry["retain_days"]),  # type: ignore[arg-type]
                )
                if getattr(res, "deleted_rows", 0):
                    log.info(
                        "retention_pruned table=%s rows=%d retain_days=%s",
                        entry["table"],
                        res.deleted_rows,
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("storage_guard_error err=%s", exc)
        await _tick("retention", _RETENTION_INTERVAL_SEC)


async def _deadman_loop() -> None:
    """§6 死人开关:内部循环存活评估(deadman_evaluate) + L1 外部心跳(heartbeat_emit).

    - 内部:对每个受监督循环,若曾见但现静默超 interval×factor+startup_grace ⇒ 卡死,大声 error。
    - 外部(L1):仅当所有循环健康时才向 cfg.deadman_heartbeat_url 发心跳;任一卡死则**抑制**心跳
      → 外部 watcher 超时告警("谁看门人":aegis 自身失能由平台外部发现,不自证清白)。
    URL 空 = 外部死人禁用(degraded,仅内部 error 日志)。heartbeat_emit 是 sync → to_thread。"""
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from oprim import heartbeat_emit  # noqa: PLC0415
    from oskill.deadman_evaluate import deadman_evaluate  # noqa: PLC0415

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
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from oprim import pg_advisory_lock_plan  # noqa: PLC0415

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
            _alert_eval_loop(),
            _retention_loop(),
            _deadman_loop(),
            _self_backup_loop(),
            return_exceptions=True,
        )
    finally:
        from aegis.server.persistence import get_pool  # noqa: PLC0415
        from oprim import pg_advisory_lock_plan  # noqa: PLC0415

        try:
            await runner_conn.fetchval(
                "SELECT pg_advisory_unlock($1)",
                pg_advisory_lock_plan(name=_LOOP_RUNNER_ROLE).key,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            await get_pool().release(runner_conn)
        except Exception:  # noqa: BLE001
            pass


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
