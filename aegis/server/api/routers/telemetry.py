"""Distributed tracing / APM — OTLP trace ingestion + query.

Apps point their OpenTelemetry OTLP/HTTP exporter at
``/api/v1/telemetry/{org_id}/v1/traces``; spans are stored and turned into
per-service RED (rate / errors / duration), recent traces, span waterfalls,
and an auto-discovered service dependency map. Self-hosted, no external APM
backend.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import get_pool
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

# OTLP exporters POST to an org-scoped URL (auth is still the shared ingest
# key below — org_id in the path is only used to stamp rows, not to authenticate).
# Ingestion is a separate router since it has no user session / RBAC.
ingest_router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])
router = APIRouter(prefix="/api/v1/orgs/{org_id}/telemetry", tags=["telemetry"])

_RETENTION_HOURS = 48
_ingest_count = 0


def _attr_map(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in attrs or []:
        v = a.get("value", {})
        out[a.get("key", "")] = (
            v.get("stringValue") or v.get("intValue") or v.get("boolValue") or v.get("doubleValue")
        )
    return out


@ingest_router.post("/{org_id}/v1/traces")
async def ingest_traces(
    org_id: uuid.UUID,
    request: Request,
    x_aegis_ingest_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """OTLP/HTTP JSON trace ingest. If AEGIS_TELEMETRY_INGEST_KEY is set it must
    match the X-Aegis-Ingest-Key header. ``org_id`` is only used to stamp the
    stored rows — the shared ingest key remains the sole auth mechanism."""
    global _ingest_count  # noqa: PLW0603
    key = getattr(get_settings(), "telemetry_ingest_key", "") or ""
    if key and x_aegis_ingest_key != key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad ingest key")

    body = await request.json()
    rows: list[tuple[Any, ...]] = []
    for rs in body.get("resourceSpans", []):
        svc = _attr_map(rs.get("resource", {}).get("attributes")).get("service.name") or "unknown"
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                try:
                    start = int(sp.get("startTimeUnixNano", 0))
                    end = int(sp.get("endTimeUnixNano", 0))
                    rows.append(
                        (
                            sp.get("traceId", ""),
                            sp.get("spanId", ""),
                            sp.get("parentSpanId") or None,
                            str(svc),
                            sp.get("name", ""),
                            int(sp.get("kind", 0) or 0),
                            start,
                            max(0, end - start),
                            int((sp.get("status") or {}).get("code", 0) or 0),
                            org_id,
                        )
                    )
                except Exception:  # noqa: BLE001 — skip malformed spans
                    continue
    if rows:
        async with get_pool().acquire() as conn:
            await conn.executemany(
                """INSERT INTO aegis_spans
                   (trace_id, span_id, parent_span_id, service, name, kind,
                    start_ns, duration_ns, status_code, org_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                rows,
            )
            _ingest_count += 1
            if _ingest_count % 200 == 0:  # opportunistic retention
                await conn.execute(
                    "DELETE FROM aegis_spans WHERE ingested_at < now() - "
                    f"interval '{_RETENTION_HOURS} hours'"
                )
    return {"accepted": len(rows)}


@ingest_router.post("/{org_id}/rum")
async def ingest_rum(org_id: uuid.UUID, request: Request) -> dict[str, str]:
    """RUM beacon ingest — a browser snippet POSTs page-load timing here."""
    b = await request.json()
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO aegis_rum (app, page, load_ms, ttfb_ms, fcp_ms, ua, org_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            str(b.get("app") or "web")[:120],
            str(b.get("page") or "/")[:300],
            _num(b.get("load_ms")),
            _num(b.get("ttfb_ms")),
            _num(b.get("fcp_ms")),
            str(b.get("ua") or "")[:300] or None,
            org_id,
        )
    return {"status": "ok"}


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/rum")
async def rum_metrics(
    org_id: uuid.UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Real-user page-load metrics per page: sample count, p50/p95 load (ms)."""
    rows = await conn.fetch(
        """
        SELECT app, page, count(*) AS views,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY load_ms)  AS p50,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY load_ms) AS p95
          FROM aegis_rum
         WHERE ingested_at > now() - ($1 || ' minutes')::interval AND load_ms IS NOT NULL
           AND org_id = $2
         GROUP BY app, page ORDER BY views DESC LIMIT 100
        """,
        str(minutes),
        org_id,
    )
    return [
        {
            "app": r["app"],
            "page": r["page"],
            "views": r["views"],
            "p50_ms": round(r["p50"] or 0, 1),
            "p95_ms": round(r["p95"] or 0, 1),
        }
        for r in rows
    ]


@router.get("/services")
async def services(
    org_id: uuid.UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Per-service RED: request rate, error %, p50/p95/p99 latency (ms)."""
    rows = await conn.fetch(
        f"""
        SELECT service,
               count(*)                                              AS calls,
               (sum(CASE WHEN status_code = 2 THEN 1 ELSE 0 END)::float
                    / count(*)) * 100                                AS error_pct,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ns)  AS p50,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ns) AS p95,
               percentile_disc(0.99) WITHIN GROUP (ORDER BY duration_ns) AS p99
          FROM aegis_spans
         WHERE ingested_at > now() - ($1 || ' minutes')::interval
           AND org_id = $2
         GROUP BY service
         ORDER BY calls DESC
        """,
        str(minutes),
        org_id,
    )
    return [
        {
            "service": r["service"],
            "calls": r["calls"],
            "rpm": round(r["calls"] / minutes, 2),
            "error_pct": round(r["error_pct"], 2),
            "p50_ms": round((r["p50"] or 0) / 1e6, 2),
            "p95_ms": round((r["p95"] or 0) / 1e6, 2),
            "p99_ms": round((r["p99"] or 0) / 1e6, 2),
        }
        for r in rows
    ]


@router.get("/traces")
async def traces(
    org_id: uuid.UUID,
    service: str | None = Query(default=None),
    minutes: int = Query(default=60, ge=1, le=1440),
    limit: int = Query(default=50, ge=1, le=200),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Recent traces (one row per trace: root service/name, total duration, error)."""
    rows = await conn.fetch(
        """
        WITH roots AS (
            SELECT DISTINCT ON (trace_id) trace_id, service, name, duration_ns,
                   status_code, ingested_at
              FROM aegis_spans
             WHERE ingested_at > now() - ($1 || ' minutes')::interval
               AND ($2::text IS NULL OR service = $2)
               AND org_id = $4
             ORDER BY trace_id, parent_span_id NULLS FIRST, start_ns
        )
        SELECT r.trace_id, r.service, r.name, r.status_code, r.ingested_at,
               (SELECT max(start_ns + duration_ns) - min(start_ns)
                  FROM aegis_spans s WHERE s.trace_id = r.trace_id AND s.org_id = $4) AS total_ns
          FROM roots r
         ORDER BY r.ingested_at DESC
         LIMIT $3
        """,
        str(minutes),
        service,
        limit,
        org_id,
    )
    return [
        {
            "trace_id": r["trace_id"],
            "root_service": r["service"],
            "root_name": r["name"],
            "error": r["status_code"] == 2,
            "duration_ms": round((r["total_ns"] or 0) / 1e6, 2),
            "at": r["ingested_at"].isoformat(),
        }
        for r in rows
    ]


@router.get("/traces/{trace_id}")
async def trace_detail(
    org_id: uuid.UUID,
    trace_id: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """All spans of a trace for a waterfall view."""
    rows = await conn.fetch(
        """SELECT span_id, parent_span_id, service, name, kind, start_ns,
                  duration_ns, status_code
             FROM aegis_spans WHERE trace_id = $1 AND org_id = $2 ORDER BY start_ns""",
        trace_id,
        org_id,
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trace not found")
    t0 = min(r["start_ns"] for r in rows)
    return {
        "trace_id": trace_id,
        "spans": [
            {
                "span_id": r["span_id"],
                "parent_span_id": r["parent_span_id"],
                "service": r["service"],
                "name": r["name"],
                "kind": r["kind"],
                "offset_ms": round((r["start_ns"] - t0) / 1e6, 2),
                "duration_ms": round(r["duration_ns"] / 1e6, 2),
                "error": r["status_code"] == 2,
            }
            for r in rows
        ],
    }


@router.get("/rca")
async def root_cause(
    org_id: uuid.UUID,
    service: str = Query(..., description="the impacted service"),
    minutes: int = Query(default=60, ge=1, le=1440),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Deterministic, topology-driven root-cause ranking: walk the dependency
    graph downstream of *service* and rank each reachable service by error rate.
    The likeliest root is the deepest downstream service that is itself erroring."""
    edges = await conn.fetch(
        """SELECT DISTINCT p.service AS src, c.service AS dst
             FROM aegis_spans c JOIN aegis_spans p ON c.parent_span_id = p.span_id
            WHERE c.ingested_at > now() - ($1 || ' minutes')::interval AND c.service <> p.service
              AND c.org_id = $2 AND p.org_id = $2""",
        str(minutes),
        org_id,
    )
    graph: dict[str, list[str]] = {}
    for e in edges:
        graph.setdefault(e["src"], []).append(e["dst"])

    # BFS downstream from the impacted service, tracking depth.
    depth: dict[str, int] = {service: 0}
    queue = [service]
    while queue:
        cur = queue.pop(0)
        for nxt in graph.get(cur, []):
            if nxt not in depth:
                depth[nxt] = depth[cur] + 1
                queue.append(nxt)

    err = await conn.fetch(
        """SELECT service,
                  (sum(CASE WHEN status_code=2 THEN 1 ELSE 0 END)::float / count(*)) * 100 AS error_pct,
                  count(*) AS calls
             FROM aegis_spans
            WHERE ingested_at > now() - ($1 || ' minutes')::interval AND service = ANY($2::text[])
              AND org_id = $3
            GROUP BY service""",
        str(minutes),
        list(depth.keys()),
        org_id,
    )
    cand = [
        {
            "service": r["service"],
            "depth": depth.get(r["service"], 0),
            "error_pct": round(r["error_pct"], 2),
            "calls": r["calls"],
            # deeper + more errors ⇒ likelier root cause
            "score": round(r["error_pct"] * (1 + depth.get(r["service"], 0)), 2),
        }
        for r in err
        if r["error_pct"] > 0
    ]
    cand.sort(key=lambda c: c["score"], reverse=True)
    return {
        "impacted": service,
        "likely_root": cand[0]["service"] if cand else None,
        "candidates": cand,
    }


@router.get("/topology")
async def topology(
    org_id: uuid.UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Auto-discovered service dependency map from parent→child span services."""
    edges = await conn.fetch(
        """
        SELECT p.service AS src, c.service AS dst, count(*) AS calls,
               (sum(CASE WHEN c.status_code = 2 THEN 1 ELSE 0 END)::float
                    / count(*)) * 100 AS error_pct
          FROM aegis_spans c
          JOIN aegis_spans p ON c.parent_span_id = p.span_id
         WHERE c.ingested_at > now() - ($1 || ' minutes')::interval
           AND c.service <> p.service
           AND c.org_id = $2 AND p.org_id = $2
         GROUP BY p.service, c.service
        """,
        str(minutes),
        org_id,
    )
    nodes = await conn.fetch(
        """SELECT DISTINCT service FROM aegis_spans
             WHERE ingested_at > now() - ($1 || ' minutes')::interval AND org_id = $2""",
        str(minutes),
        org_id,
    )
    return {
        "nodes": [n["service"] for n in nodes],
        "edges": [
            {
                "src": e["src"],
                "dst": e["dst"],
                "calls": e["calls"],
                "error_pct": round(e["error_pct"], 2),
            }
            for e in edges
        ],
    }
