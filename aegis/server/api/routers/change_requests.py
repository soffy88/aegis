"""Change Management API — Change Request workflow with approval gates.

AEGIS_DESIGN v1.1.0 §9
"""

from __future__ import annotations

import enum
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import record_audit, get_pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/changes", tags=["changes"])


# ── Models ────────────────────────────────────────────────────────────────

class ChangeType(str, enum.Enum):
    CONFIG_APPLY = "config.apply"
    APP_UPGRADE = "app.upgrade"
    APP_ROLLBACK = "app.rollback"
    APP_UNINSTALL = "app.uninstall"
    SECRET_ROTATE = "secret.rotate"
    CONFIG_ROLLBACK = "config.rollback"


class ChangeRequestStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class ChangeAction(BaseModel):
    type: ChangeType
    payload: dict[str, Any]
    description: str | None = None


class ChangeRequestCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    project_id: UUID | None = None
    actions: list[ChangeAction] = Field(..., min_length=1)
    approvers: list[UUID] = Field(default_factory=list)  # 指定审批人，空则按角色自动路由
    scheduled_at: datetime | None = None  # 定时执行


class ChangeRequestOut(BaseModel):
    id: UUID
    title: str
    description: str | None
    project_id: UUID | None
    status: ChangeRequestStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    approvers: list[UUID]
    approvals: dict[UUID, dict]  # {user_id: {decision, at, comment}}
    scheduled_at: datetime | None
    executed_at: datetime | None
    execution_log: list[dict]


class ApprovalAction(BaseModel):
    decision: str  # "approve" | "reject"
    comment: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("", response_model=ChangeRequestOut, status_code=status.HTTP_201_CREATED)
async def create_change_request(
    req: ChangeRequestCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> ChangeRequestOut:
    """创建变更请求 (需审批)"""
    cr_id = uuid.uuid4()
    now = datetime.now(UTC)

    # 默认审批人：项目 admin/owner
    approvers = req.approvers
    if not approvers and req.project_id:
        # 查询项目 admin/owner
        rows = await conn.fetch("""
            SELECT user_id FROM org_memberships om
            JOIN projects p ON p.org_id = om.org_id
            WHERE p.id = $1 AND om.role IN ('admin', 'owner')
        """, req.project_id)
        approvers = [r["user_id"] for r in rows]

    await conn.execute("""
        INSERT INTO change_requests
        (id, title, description, project_id, status, created_by, actions, approvers, scheduled_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """,
        cr_id,
        req.title,
        req.description,
        req.project_id,
        ChangeRequestStatus.PENDING_APPROVAL if approvers else ChangeRequestStatus.APPROVED,
        user.user_id,
        [a.model_dump() for a in req.actions],
        approvers,
        req.scheduled_at,
        now,
        now,
    )

    # 记录审计
    if user.orgs:
        await record_audit(
            conn,
            org_id=user.orgs[0].org_id,
            action="change_request.created",
            actor_user_id=user.user_id,
            target_type="change_request",
            target_id=str(cr_id),
            metadata={"title": req.title, "actions": len(req.actions)},
        )

    return await _get_cr(conn, cr_id)


@router.get("", response_model=list[ChangeRequestOut])
async def list_change_requests(
    status: ChangeRequestStatus | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> list[ChangeRequestOut]:
    """列出变更请求"""
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")

    org_id = user.orgs[0].org_id
    query = """
        SELECT cr.* FROM change_requests cr
        JOIN projects p ON cr.project_id = p.id
        WHERE p.org_id = $1
    """
    params = [org_id]
    idx = 2

    if status:
        query += f" AND cr.status = ${idx}"
        params.append(status.value)
        idx += 1
    if project_id:
        query += f" AND cr.project_id = ${idx}"
        params.append(project_id)
        idx += 1

    query += f" ORDER BY cr.created_at DESC LIMIT ${idx}"
    params.append(limit)

    rows = await conn.fetch(query, *params)
    return [_row_to_cr(r) for r in rows]


@router.get("/{cr_id}", response_model=ChangeRequestOut)
async def get_change_request(
    cr_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> ChangeRequestOut:
    """获取变更请求详情"""
    cr = await _get_cr(conn, cr_id)
    if not cr:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    return cr


@router.post("/{cr_id}/approve", response_model=ChangeRequestOut)
async def approve_change_request(
    cr_id: UUID,
    action: ApprovalAction,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> ChangeRequestOut:
    """审批变更请求"""
    cr = await _get_cr(conn, cr_id)
    if not cr:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")

    if cr["status"] not in (ChangeRequestStatus.PENDING_APPROVAL, ChangeRequestStatus.DRAFT):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot approve in status {cr['status']}")

    # 验证审批人
    if cr["approvers"] and user.user_id not in cr["approvers"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not an approver for this request")

    approvals = cr.get("approvals", {}) or {}
    approvals[str(user.user_id)] = {
        "decision": action.decision,
        "comment": action.comment,
        "at": datetime.now(UTC).isoformat(),
    }

    # 检查是否全部通过
    all_approved = True
    if cr["approvers"]:
        for ap in cr["approvers"]:
            ap_str = str(ap)
            if ap_str not in approvals:
                all_approved = False
                break
            if approvals[ap_str]["decision"] != "approve":
                all_approved = False
                break

    new_status = ChangeRequestStatus.APPROVED if all_approved else ChangeRequestStatus.PENDING_APPROVAL
    if action.decision == "reject":
        new_status = ChangeRequestStatus.REJECTED

    await conn.execute("""
        UPDATE change_requests SET status = $2, approvals = $3, updated_at = $4
        WHERE id = $1
    """, cr_id, new_status.value, approvals, datetime.now(UTC))

    # 审计
    if user.orgs:
        await record_audit(
            conn,
            org_id=user.orgs[0].org_id,
            action="change_request.approved" if action.decision == "approve" else "change_request.rejected",
            actor_user_id=user.user_id,
            target_type="change_request",
            target_id=str(cr_id),
            metadata={"comment": action.comment},
        )

    return await _get_cr(conn, cr_id)


@router.post("/{cr_id}/execute", response_model=ChangeRequestOut)
async def execute_change_request(
    cr_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> ChangeRequestOut:
    """执行已批准的变更请求"""
    cr = await _get_cr(conn, cr_id)
    if not cr:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")

    if cr["status"] != ChangeRequestStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Change request not approved (status: {cr['status']})")

    # 标记执行中
    await conn.execute("""
        UPDATE change_requests SET status = $2, executed_at = $3, updated_at = $3
        WHERE id = $1
    """, cr_id, ChangeRequestStatus.EXECUTING.value, datetime.now(UTC))

    execution_log = []
    success = True

    try:
        for action in cr["actions"]:
            # TODO: 执行具体动作 (调用相应 API)
            log_entry = {
                "action": action["type"],
                "status": "started",
                "at": datetime.now(UTC).isoformat(),
            }
            execution_log.append(log_entry)

            # 这里应该调用实际的执行器
            # 例如: await config_executor.apply(...)
            # 暂时标记成功
            log_entry["status"] = "completed"
            log_entry["completed_at"] = datetime.now(UTC).isoformat()

        # 全部成功
        await conn.execute("""
            UPDATE change_requests SET status = $2, execution_log = $3, updated_at = $4
            WHERE id = $1
        """, cr_id, ChangeRequestStatus.COMPLETED.value, execution_log, datetime.now(UTC))

    except Exception as e:
        success = False
        log.error("change_execution_failed cr_id=%s err=%s", cr_id, e)
        await conn.execute("""
            UPDATE change_requests SET status = $2, execution_log = $3, updated_at = $4
            WHERE id = $1
        """, cr_id, ChangeRequestStatus.FAILED.value, execution_log, datetime.now(UTC))

    # 审计
    if user.orgs:
        await record_audit(
            conn,
            org_id=user.orgs[0].org_id,
            action="change_request.executed",
            actor_user_id=user.user_id,
            target_type="change_request",
            target_id=str(cr_id),
            metadata={"success": success, "actions": len(cr["actions"])},
        )

    return await _get_cr(conn, cr_id)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _get_cr(conn: asyncpg.Connection, cr_id: UUID) -> ChangeRequestOut | None:
    row = await conn.fetchrow("SELECT * FROM change_requests WHERE id = $1", cr_id)
    if not row:
        return None
    return _row_to_cr(row)


def _row_to_cr(row: asyncpg.Record) -> ChangeRequestOut:
    return ChangeRequestOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        project_id=row["project_id"],
        status=ChangeRequestStatus(row["status"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approvers=row["approvers"] or [],
        approvals=row["approvals"] or {},
        scheduled_at=row["scheduled_at"],
        executed_at=row["executed_at"],
        execution_log=row["execution_log"] or [],
    )