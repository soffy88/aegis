"""Autoheal-policy management API (org-scoped).

Each policy binds a container to a trigger (metric/operator/threshold) + restart
action, with dry_run (default true) + cooldown. The autoheal cron evaluates them.
viewer+ can list; operator+ (TRIGGER_AUTOHEAL) can manage. Creating a policy with
dry_run=false authorizes real, unattended container restarts on breach.
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

router = APIRouter(prefix="/api/v1/orgs/{org_id}/autoheal-policies", tags=["autoheal-policies"])

_OP = Literal[">=", ">", "<", "<=", "=="]


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_container: str = Field(min_length=1, max_length=200)
    trigger_metric: str = Field(min_length=1, max_length=200)
    trigger_operator: _OP = "<"
    trigger_threshold: float
    action: Literal["restart"] = "restart"
    dry_run: bool = True
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    docker_host: str | None = None
    enabled: bool = True


class PolicyUpdate(BaseModel):
    target_container: str | None = Field(default=None, max_length=200)
    trigger_metric: str | None = Field(default=None, max_length=200)
    trigger_operator: _OP | None = None
    trigger_threshold: float | None = None
    dry_run: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=30, le=86400)
    enabled: bool | None = None


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    return d


@router.get("")
async def list_policies(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, name, target_container, trigger_metric, trigger_operator,"
        " trigger_threshold, action, dry_run, cooldown_seconds, enabled, last_triggered_at"
        " FROM autoheal_policies WHERE org_id = $1 ORDER BY name",
        org_id,
    )
    return [_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy(
    org_id: uuid.UUID,
    body: PolicyCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        r = await conn.fetchrow(
            "INSERT INTO autoheal_policies"
            " (org_id, name, target_container, trigger_metric, trigger_operator,"
            "  trigger_threshold, action, dry_run, cooldown_seconds, docker_host, enabled)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *",
            org_id, body.name, body.target_container, body.trigger_metric, body.trigger_operator,
            body.trigger_threshold, body.action, body.dry_run, body.cooldown_seconds,
            body.docker_host, body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"policy '{body.name}' already exists") from exc
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
        r = await conn.fetchrow("SELECT * FROM autoheal_policies WHERE org_id=$1 AND id=$2", org_id, policy_id)
    else:
        cols = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(updates))
        r = await conn.fetchrow(
            f"UPDATE autoheal_policies SET {cols} WHERE org_id=$1 AND id=$2 RETURNING *",
            org_id, policy_id, *updates.values(),
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
    res = await conn.execute("DELETE FROM autoheal_policies WHERE org_id=$1 AND id=$2", org_id, policy_id)
    if res == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")
