"""POST /api/v1/metrics/ingest — receive metric batches from aegis-agent."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.runtime.config import AegisSettings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


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
    if token != cfg.agent_token:
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
