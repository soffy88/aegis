"""POST /api/v1/metrics/ingest — receive metric batches from aegis-agent."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.runtime.config import AegisSettings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])

# Whitelisted aggregation functions (prevents SQL injection via the agg param).
_AGG_FUNCS = {"avg", "max", "min", "sum"}


async def prune_old_metrics(conn: asyncpg.Connection, retention_days: int) -> int:
    """Delete agent_metrics rows older than retention_days. Returns rows removed.

    Called from the orchestration cron. retention_days<=0 disables pruning.
    """
    if retention_days <= 0:
        return 0
    result = await conn.execute(
        "DELETE FROM agent_metrics WHERE ts < now() - ($1::double precision * interval '1 day')",
        float(retention_days),
    )
    # asyncpg returns e.g. "DELETE 42"
    try:
        removed = int(result.split()[-1])
    except (ValueError, IndexError):
        removed = 0
    if removed:
        log.info("agent_metrics_pruned removed=%d retention_days=%d", removed, retention_days)
    return removed


class MetricPoint(BaseModel):
    name: str
    value: float
    unit: str = ""
    tags: dict[str, Any] = Field(default_factory=dict)


class MetricsIngestRequest(BaseModel):
    hostname: str
    collected_at: str
    metrics: list[MetricPoint]


def _verify_agent_token(
    cfg: AegisSettings,
    authorization: str | None,
) -> None:
    """Verify Bearer token if cfg.agent_token is set. Skips check when empty (dev)."""
    if not cfg.agent_token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, cfg.agent_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent token",
        )


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_metrics(
    body: MetricsIngestRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    cfg: AegisSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Accept a metric batch from aegis-agent.

    Auth: Bearer token matching AEGIS_AGENT_TOKEN (skipped if unset — dev/test).
    """
    _verify_agent_token(cfg, authorization)

    if not body.metrics:
        return {"accepted": 0, "hostname": body.hostname}

    rows = [(body.hostname, m.name, m.value, m.unit, m.tags) for m in body.metrics]
    await conn.executemany(
        """
        INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        rows,
    )
    log.info(
        "metrics_ingested hostname=%s count=%d collected_at=%s",
        body.hostname,
        len(rows),
        body.collected_at,
    )
    return {"accepted": len(rows), "hostname": body.hostname}


@router.get("/series")
async def list_series(
    hours: float = Query(default=24, gt=0, le=24 * 90),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Distinct (hostname, metric_name) seen in the window — for chart pickers.

    Host-level infra metrics are not org-scoped; any authenticated user may read.
    """
    rows = await conn.fetch(
        "SELECT hostname, metric_name, max(unit) AS unit, count(*) AS samples"
        " FROM agent_metrics"
        " WHERE ts >= now() - ($1::double precision * interval '1 hour')"
        " GROUP BY hostname, metric_name"
        " ORDER BY hostname, metric_name",
        float(hours),
    )
    return [dict(r) for r in rows]


@router.get("/query")
async def query_metrics(
    metric_name: str = Query(..., min_length=1, max_length=200),
    hostname: str | None = Query(default=None, max_length=255),
    hours: float = Query(default=6, gt=0, le=24 * 90),
    bucket_seconds: int = Query(default=300, ge=10, le=86400),
    agg: str = Query(default="avg"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Down-sampled time series for one metric.

    Buckets are aligned to epoch and aggregated with `agg` (avg/max/min/sum).
    Returns points as {ts: ISO8601, value: float}.
    """
    if agg not in _AGG_FUNCS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agg must be one of {sorted(_AGG_FUNCS)}",
        )

    params: list[object] = [metric_name, float(hours), bucket_seconds]
    host_clause = ""
    if hostname:
        params.append(hostname)
        host_clause = f" AND hostname = ${len(params)}"

    # Bucket index → bucket-start epoch; agg is whitelisted above so safe to inline.
    sql = (
        f"SELECT (floor(extract(epoch from ts) / $3) * $3)::bigint AS bucket_epoch,"
        f" {agg}(value) AS value"
        f" FROM agent_metrics"
        f" WHERE metric_name = $1"
        f" AND ts >= now() - ($2::double precision * interval '1 hour'){host_clause}"
        f" GROUP BY bucket_epoch ORDER BY bucket_epoch"
    )
    rows = await conn.fetch(sql, *params)
    points = [
        {
            "ts": datetime.fromtimestamp(int(r["bucket_epoch"]), tz=UTC).isoformat(),
            "value": float(r["value"]),
        }
        for r in rows
    ]
    return {
        "metric_name": metric_name,
        "hostname": hostname,
        "bucket_seconds": bucket_seconds,
        "agg": agg,
        "points": points,
    }
