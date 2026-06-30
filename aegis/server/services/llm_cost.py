"""Persistent LLM cost ledger — turns per-invocation cost_usd into per-org spend.

The OmodulDispatcher already computes ``cost_usd`` for each Brain invocation (and
deducts it from the Redis budget). This module additionally persists each charge so
spend is queryable over time and per org — replacing the previous "no accounting".
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


async def record_cost(
    *,
    principal: str,
    omodul_name: str,
    model: str,
    cost_usd: float,
) -> None:
    """Append one charge to the ledger. Best-effort (never raises; uses the pool)."""
    if not cost_usd or cost_usd <= 0:
        return
    try:
        from aegis.server.persistence.db import get_pool  # noqa: PLC0415

        async with get_pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO llm_cost_ledger (principal, omodul_name, model, cost_usd)"
                " VALUES ($1, $2, $3, $4)",
                principal,
                omodul_name,
                model or "",
                float(cost_usd),
            )
    except Exception as exc:  # noqa: BLE001 — cost accounting must not break dispatch
        log.warning("llm_cost_record_failed principal=%s err=%s", principal, exc)


async def org_spend(
    conn: asyncpg.Connection,
    *,
    org_id: Any,
    days: float = 30.0,
) -> dict[str, Any]:
    """Total + per-model LLM spend for an org over the last `days`."""
    principal = str(org_id)
    total = await conn.fetchval(
        "SELECT COALESCE(sum(cost_usd), 0) FROM llm_cost_ledger"
        " WHERE principal = $1 AND created_at >= now() - ($2::double precision * interval '1 day')",
        principal,
        days,
    )
    by_model = await conn.fetch(
        "SELECT model, sum(cost_usd) AS usd, count(*) AS calls FROM llm_cost_ledger"
        " WHERE principal = $1 AND created_at >= now() - ($2::double precision * interval '1 day')"
        " GROUP BY model ORDER BY usd DESC",
        principal,
        days,
    )
    return {
        "org_id": principal,
        "days": days,
        "total_usd": round(float(total), 6),
        "by_model": [
            {"model": r["model"], "usd": round(float(r["usd"]), 6), "calls": r["calls"]}
            for r in by_model
        ],
    }
