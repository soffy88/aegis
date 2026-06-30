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


def _jittered(interval: float) -> float:
    """±10% jitter so multiple replicas don't synchronize onto the DB."""
    return interval * random.uniform(0.9, 1.1)


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
                # Retention: prune stale agent_metrics (hourly is fine for a daily TTL).
                await prune_old_metrics(conn, get_settings().agent_metrics_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("capacity_cron_error err=%s", exc)
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
        await asyncio.sleep(_jittered(_SCRAPE_INTERVAL_SEC))


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
        await asyncio.sleep(_jittered(_AUTOHEAL_INTERVAL_SEC))


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
        await asyncio.sleep(_jittered(_UPTIME_INTERVAL_SEC))


async def _recording_loop() -> None:
    """Derive rate-based gauges (container_cpu_percent) from scraped counters so
    threshold rules can alert on CPU. Runs just behind the scrape cadence."""
    from aegis.server.persistence import get_pool  # noqa: PLC0415
    from aegis.server.services.metric_recording import (
        record_container_cpu_percent,  # noqa: PLC0415
    )

    await asyncio.sleep(random.uniform(20, 35))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await record_container_cpu_percent(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("recording_cron_error err=%s", exc)
        await asyncio.sleep(_jittered(_RECORDING_INTERVAL_SEC))


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
        await asyncio.sleep(_jittered(_ALERT_EVAL_INTERVAL_SEC))


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
        await asyncio.sleep(_jittered(_ANOMALY_INTERVAL_SEC))


async def _cron_main(alerter: Any | None) -> None:
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
        return_exceptions=True,
    )


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
