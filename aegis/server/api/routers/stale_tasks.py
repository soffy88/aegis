"""Stale-task-policy management API (org-scoped) — devplatform Phase 1.

A project declares how to reap its stuck "processing" rows: target table, status +
timestamp columns, timeout, and the action (mark failed / requeue). The reaper cron
evaluates enabled policies. dry_run defaults true — creating a policy with dry_run=false
authorizes real, unattended writes to the target table on the declared schedule.
viewer+ can list; operator+ (TRIGGER_AUTOHEAL) can manage / run.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.services.stale_task_reaper import (
    InvalidIdentifier,
    StaleTaskPolicy,
    _quote_ident,
    reap_on_connection,
)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/stale-task-policies", tags=["stale-task-policies"])

_IDENT = Field(min_length=1, max_length=200)


def _validate_idents(*names: str) -> None:
    """Reject unsafe SQL identifiers up front (they're interpolated, not parameterized)."""
    for n in names:
        try:
            _quote_ident(n)
        except InvalidIdentifier as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_dsn_secret: str | None = Field(default=None, max_length=200)
    target_table: str = _IDENT
    status_column: str = _IDENT
    timestamp_column: str = _IDENT
    id_column: str | None = Field(default=None, max_length=200)
    processing_value: str = Field(min_length=1, max_length=200)
    timeout_minutes: int = Field(default=30, ge=1, le=100_000)
    action: Literal["mark_failed", "requeue"] = "mark_failed"
    failed_value: str = Field(default="failed", max_length=200)
    requeue_value: str = Field(default="pending", max_length=200)
    max_actions_per_run: int = Field(default=100, ge=1, le=100_000)
    dry_run: bool = True
    enabled: bool = True


class PolicyUpdate(BaseModel):
    timeout_minutes: int | None = Field(default=None, ge=1, le=100_000)
    action: Literal["mark_failed", "requeue"] | None = None
    failed_value: str | None = Field(default=None, max_length=200)
    requeue_value: str | None = Field(default=None, max_length=200)
    max_actions_per_run: int | None = Field(default=None, ge=1, le=100_000)
    dry_run: bool | None = None
    enabled: bool | None = None


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    d["org_id"] = str(d["org_id"])
    return d


@router.get("")
async def list_policies(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM stale_task_policies WHERE org_id = $1 ORDER BY name", org_id
    )
    return [_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy(
    org_id: uuid.UUID,
    body: PolicyCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    _validate_idents(body.target_table, body.status_column, body.timestamp_column)
    if body.id_column:
        _validate_idents(body.id_column)
    try:
        r = await conn.fetchrow(
            "INSERT INTO stale_task_policies"
            " (org_id, name, target_dsn_secret, target_table, status_column, timestamp_column,"
            "  id_column, processing_value, timeout_minutes, action, failed_value, requeue_value,"
            "  max_actions_per_run, dry_run, enabled)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING *",
            org_id,
            body.name,
            body.target_dsn_secret,
            body.target_table,
            body.status_column,
            body.timestamp_column,
            body.id_column,
            body.processing_value,
            body.timeout_minutes,
            body.action,
            body.failed_value,
            body.requeue_value,
            body.max_actions_per_run,
            body.dry_run,
            body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"policy '{body.name}' already exists"
        ) from exc
    return _row(r)


@router.patch("/{policy_id}")
async def update_policy(
    org_id: uuid.UUID,
    policy_id: uuid.UUID,
    body: PolicyUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        r = await conn.fetchrow(
            "SELECT * FROM stale_task_policies WHERE org_id=$1 AND id=$2", org_id, policy_id
        )
    else:
        cols = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(updates))
        r = await conn.fetchrow(
            f"UPDATE stale_task_policies SET {cols} WHERE org_id=$1 AND id=$2 RETURNING *",
            org_id,
            policy_id,
            *updates.values(),
        )
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")
    return _row(r)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    org_id: uuid.UUID,
    policy_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> None:
    res = await conn.execute(
        "DELETE FROM stale_task_policies WHERE org_id=$1 AND id=$2", org_id, policy_id
    )
    if res == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")


@router.get("/{policy_id}/events")
async def list_reap_events(
    org_id: uuid.UUID,
    policy_id: uuid.UUID,
    limit: int = 50,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Recent reap events for a policy (audit / decision_trail view)."""
    rows = await conn.fetch(
        "SELECT id, reaped_at, stuck_count, action, dry_run, detail"
        " FROM stale_task_reap_events WHERE policy_id=$1 AND org_id=$2"
        " ORDER BY reaped_at DESC LIMIT $3",
        policy_id,
        org_id,
        min(max(limit, 1), 500),
    )
    return [{**dict(r), "id": str(r["id"])} for r in rows]


@router.post("/{policy_id}/run")
async def run_policy_now(
    org_id: uuid.UUID,
    policy_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Run one policy immediately (honors its dry_run). Useful to preview what would be
    reaped before enabling non-dry-run."""
    from aegis.server.services.stale_task_reaper import _record, _target_connection

    row = await conn.fetchrow(
        "SELECT * FROM stale_task_policies WHERE org_id=$1 AND id=$2", org_id, policy_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")
    policy = StaleTaskPolicy.from_row(row)
    try:
        async with _target_connection(conn, policy) as target:
            result = await reap_on_connection(target, policy)
    except Exception as exc:  # noqa: BLE001 — surface, don't 500
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"reap failed: {exc}") from exc
    await _record(conn, policy, result)
    return {
        "policy": policy.name,
        "stuck_count": result.stuck_count,
        "acted": result.acted,
        "dry_run": policy.dry_run,
        "action": result.action,
        "sample_ids": result.sample_ids,
    }
