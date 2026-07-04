"""HTTP uptime-target management API (org-scoped).

Targets are HTTP endpoints the uptime prober checks; results become
probe_up/probe_latency_ms metrics. viewer+ can list; operator+ can manage.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/uptime-targets", tags=["uptime-targets"])


def _v_url(v: str) -> str:
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    return v


class UptimeTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    expected_status: int = Field(default=200, ge=100, le=599)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _v_url(v)


class UptimeTargetUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    expected_status: int | None = Field(default=None, ge=100, le=599)
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _v_url(v) if v is not None else v


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    return d


@router.get("")
async def list_targets(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, name, url, interval_seconds, expected_status, enabled,"
        " last_up, last_latency_ms, last_checked_at, last_error, last_tls_days_remaining"
        " FROM uptime_targets WHERE org_id = $1 ORDER BY name",
        org_id,
    )
    return [_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_target(
    org_id: uuid.UUID,
    body: UptimeTargetCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        r = await conn.fetchrow(
            "INSERT INTO uptime_targets (org_id, name, url, interval_seconds, expected_status, enabled)"
            " VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            org_id,
            body.name,
            body.url,
            body.interval_seconds,
            body.expected_status,
            body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"target '{body.name}' already exists"
        ) from exc
    return _row(r)


@router.patch("/{target_id}")
async def update_target(
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    body: UptimeTargetUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        r = await conn.fetchrow(
            "SELECT * FROM uptime_targets WHERE org_id=$1 AND id=$2", org_id, target_id
        )
    else:
        cols = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(updates))
        r = await conn.fetchrow(
            f"UPDATE uptime_targets SET {cols} WHERE org_id=$1 AND id=$2 RETURNING *",
            org_id,
            target_id,
            *updates.values(),
        )
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target not found")
    return _row(r)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> None:
    res = await conn.execute(
        "DELETE FROM uptime_targets WHERE org_id=$1 AND id=$2", org_id, target_id
    )
    if res == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target not found")
