"""Alert rules CRUD router."""

from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
from aegis.server.schemas.alerting import AlertRuleCreate, AlertRuleResponse, AlertRuleUpdate

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules",
    tags=["alert-rules"],
)


@router.get("", response_model=list[AlertRuleResponse])
async def list_alert_rules(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    enabled_only: bool = Query(False),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[AlertRuleResponse]:
    repo = AlertRuleRepository(conn)
    return await repo.list_by_project(
        org_id=org_id, project_id=project_id, enabled_only=enabled_only
    )


@router.post("", response_model=AlertRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_alert_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    data: AlertRuleCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> AlertRuleResponse:
    repo = AlertRuleRepository(conn)
    try:
        return await repo.create(
            org_id=org_id,
            project_id=project_id,
            created_by=user.user_id,
            data=data,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "alert rule name already exists in this project"
        ) from exc


@router.get("/{rule_id}", response_model=AlertRuleResponse)
async def get_alert_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> AlertRuleResponse:
    repo = AlertRuleRepository(conn)
    rule = await repo.get(rule_id=rule_id, org_id=org_id)
    if not rule or rule.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert rule not found")
    return rule


@router.patch("/{rule_id}", response_model=AlertRuleResponse)
async def update_alert_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    data: AlertRuleUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> AlertRuleResponse:
    repo = AlertRuleRepository(conn)
    rule = await repo.update(rule_id=rule_id, org_id=org_id, data=data)
    if not rule or rule.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert rule not found")
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> None:
    repo = AlertRuleRepository(conn)
    if not await repo.delete(rule_id=rule_id, org_id=org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert rule not found")
