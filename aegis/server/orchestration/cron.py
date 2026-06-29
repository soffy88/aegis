"""Orchestration cron scheduler.

Runs two background loops:
- Event correlator: every 5 min
- Capacity check:  every 60 min
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

log = logging.getLogger(__name__)

_CORRELATOR_INTERVAL_SEC = 300  # 5 min
_CAPACITY_INTERVAL_SEC = 3600  # 60 min


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
    from aegis.server.orchestration.capacity import run_capacity_check  # noqa: PLC0415
    from aegis.server.persistence import get_pool  # noqa: PLC0415

    await asyncio.sleep(random.uniform(30, 60))
    while True:
        try:
            async with get_pool().acquire() as conn:
                await run_capacity_check(conn=conn, alerter=alerter)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("capacity_cron_error err=%s", exc)
        await asyncio.sleep(_jittered(_CAPACITY_INTERVAL_SEC))


async def _cron_main(alerter: Any | None) -> None:
    await asyncio.gather(
        _correlator_loop(),
        _capacity_loop(alerter),
        return_exceptions=True,
    )


def start_orchestration_crons(alerter: Any | None = None) -> asyncio.Task:
    """Start both cron loops as a single background task."""
    task = asyncio.ensure_future(_cron_main(alerter))
    log.info(
        "orchestration_crons_started correlator=%ds capacity=%ds",
        _CORRELATOR_INTERVAL_SEC,
        _CAPACITY_INTERVAL_SEC,
    )
    return task
