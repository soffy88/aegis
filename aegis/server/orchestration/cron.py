"""Orchestration cron scheduler.

Runs three background loops:
- Event correlator:  every 5 min
- Capacity check:    every 60 min
- Alert escalation:  every 2 min
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


async def _cron_main(alerter: Any | None) -> None:
    await asyncio.gather(
        _correlator_loop(),
        _capacity_loop(alerter),
        _escalation_loop(),
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
