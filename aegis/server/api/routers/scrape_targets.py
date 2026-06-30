"""Prometheus scrape-target management API (org-scoped).

Targets are HTTP endpoints exposing the Prometheus text format; the scrape cron
pulls them into agent_metrics. viewer+ can list; operator+ can manage.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/scrape-targets", tags=["scrape-targets"])


def _validate_scrape_url(v: str) -> str:
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    host = urllib.parse.urlparse(v).hostname or ""
    if not host:
        raise ValueError("url must include a host")
    # Scrape targets are intentionally internal (localhost exporters, private IPs),
    # so we do NOT block private ranges — only reject obviously invalid hosts.
    try:
        ipaddress.ip_address(host)  # ok if it parses; non-IP hostnames also fine
    except ValueError:
        pass
    return v


class ScrapeTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    interval_seconds: int = Field(default=30, ge=5, le=3600)
    labels: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str) -> str:
        return _validate_scrape_url(v)


class ScrapeTargetUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    labels: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str | None) -> str | None:
        return _validate_scrape_url(v) if v is not None else v


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    d["org_id"] = str(d["org_id"])
    return d


@router.get("")
async def list_targets(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM scrape_targets WHERE org_id = $1 ORDER BY name", org_id
    )
    return [_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_target(
    org_id: uuid.UUID,
    body: ScrapeTargetCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> dict[str, Any]:
    try:
        row = await conn.fetchrow(
            "INSERT INTO scrape_targets (org_id, name, url, interval_seconds, labels, enabled)"
            " VALUES ($1,$2,$3,$4,$5::jsonb,$6) RETURNING *",
            org_id,
            body.name,
            body.url,
            body.interval_seconds,
            json.dumps(body.labels),
            body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"scrape target named {body.name!r} already exists"
        ) from exc
    return _row(row)


@router.patch("/{target_id}")
async def update_target(
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    body: ScrapeTargetUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        row = await conn.fetchrow(
            "SELECT * FROM scrape_targets WHERE org_id=$1 AND id=$2", org_id, target_id
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "scrape target not found")
        return _row(row)
    if "labels" in updates:
        updates["labels"] = json.dumps(updates["labels"])
    cols = list(updates.keys())
    set_clause = ", ".join(
        f"{c} = ${i + 3}::jsonb" if c == "labels" else f"{c} = ${i + 3}"
        for i, c in enumerate(cols)
    )
    row = await conn.fetchrow(
        f"UPDATE scrape_targets SET {set_clause} WHERE org_id=$1 AND id=$2 RETURNING *",
        org_id,
        target_id,
        *updates.values(),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scrape target not found")
    return _row(row)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> None:
    result = await conn.execute(
        "DELETE FROM scrape_targets WHERE org_id=$1 AND id=$2", org_id, target_id
    )
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scrape target not found")


@router.post("/{target_id}/scrape-now")
async def scrape_now(
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> dict[str, Any]:
    """Validate + preview a target immediately (does not store)."""
    from aegis.server.services.metrics_scraper import scrape_url  # noqa: PLC0415

    row = await conn.fetchrow(
        "SELECT url FROM scrape_targets WHERE org_id=$1 AND id=$2", org_id, target_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scrape target not found")
    try:
        samples = await scrape_url(row["url"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"scrape failed: {exc}") from exc
    return {"sample_count": len(samples), "preview": [s[0] for s in samples[:20]]}
