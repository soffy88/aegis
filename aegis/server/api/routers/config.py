"""Configuration Management API — Declarative desired state CRUD + dry-run/apply/rollback.

AEGIS_DESIGN v1.1.0 §7
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.config_management import (
    ConfigExecutor,
    DesiredState,
    ExecutionResult,
    PackageSpec,
    ServiceSpec,
    FileSpec,
    SysctlSpec,
    UserSpec,
)
from aegis.server.persistence import record_audit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["config"])


# ── Request/Response Models ────────────────────────────────────────────────

class PackageSpecIn(PackageSpec):
    """Accepted from API (matches PackageSpec)"""
    pass


class ServiceSpecIn(ServiceSpec):
    pass


class FileSpecIn(FileSpec):
    pass


class SysctlSpecIn(SysctlSpec):
    pass


class UserSpecIn(UserSpec):
    pass


class DesiredStateIn(DesiredState):
    packages: list[PackageSpecIn] = []
    services: list[ServiceSpecIn] = []
    files: list[FileSpecIn] = []
    sysctl: list[SysctlSpecIn] = []
    users: list[UserSpecIn] = []


class DiffEntryOut:
    resource_type: str
    resource_name: str
    action: str
    current: dict | None
    desired: dict
    changes: dict[str, list]


class ExecutionResultOut:
    success: bool
    diffs: list[DiffEntryOut]
    applied: list[DiffEntryOut]
    failed: list[DiffEntryOut]
    error: str | None
    duration_ms: int
    rollback_available: bool


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/dryrun", response_model=ExecutionResultOut)
async def config_dryrun(
    desired: DesiredStateIn,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_DRYRUN)),
) -> ExecutionResultOut:
    """计算并返回配置差异（不修改系统）。

    权限: CONFIG_DRYRUN (admin+)
    """
    executor = ConfigExecutor(conn)
    diffs = await executor.compute_diff(desired)

    # 转换为输出格式
    def to_out(d):
        return {
            "resource_type": d.resource_type,
            "resource_name": d.resource_name,
            "action": d.action,
            "current": d.current,
            "desired": d.desired,
            "changes": {k: [v[0], v[1]] for k, v in d.changes.items()},
        }

    return {
        "success": True,
        "diffs": [to_out(d) for d in diffs],
        "applied": [],
        "failed": [],
        "error": None,
        "duration_ms": 0,
        "rollback_available": False,
    }


@router.post("/apply", response_model=ExecutionResultOut)
async def config_apply(
    desired: DesiredStateIn,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> ExecutionResultOut:
    """执行配置应用（原子性，失败回滚）。

    权限: CONFIG_APPLY (owner only)
    """
    executor = ConfigExecutor(conn)
    result = await executor.apply(desired, dry_run=False, actor_user_id=user.user_id)

    # 记录审计
    if user.orgs:
        org_id = user.orgs[0].org_id
        await record_audit(
            conn,
            org_id=org_id,
            action="config.applied",
            actor_user_id=user.user_id,
            target_type="config",
            target_id=None,
            metadata={
                "success": result.success,
                "applied_count": len(result.applied),
                "failed_count": len(result.failed),
                "duration_ms": result.duration_ms,
            },
        )

    def to_out(d):
        return {
            "resource_type": d.resource_type,
            "resource_name": d.resource_name,
            "action": d.action,
            "current": d.current,
            "desired": d.desired,
            "changes": {k: [v[0], v[1]] for k, v in d.changes.items()},
        }

    return {
        "success": result.success,
        "diffs": [to_out(d) for d in result.diffs],
        "applied": [to_out(d) for d in result.applied],
        "failed": [to_out(d) for d in result.failed],
        "error": result.error,
        "duration_ms": result.duration_ms,
        "rollback_available": result.rollback_available,
    }


@router.post("/rollback", response_model=ExecutionResultOut)
async def config_rollback(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_ROLLBACK)),
) -> ExecutionResultOut:
    """回滚到上一次配置应用前的状态。

    权限: CONFIG_ROLLBACK (owner only)
    """
    # TODO: 实现回滚逻辑 (需持久化历史快照)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Rollback not yet implemented — requires snapshot persistence",
    )


# ── Template Management (for file content) ───────────────────────────────

@router.get("/templates")
async def list_templates(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> list[dict]:
    """列出可用的 Jinja2 模板"""
    # TODO: 从模板目录扫描
    return [
        {"name": "nginx.conf.j2", "description": "Nginx reverse proxy config"},
        {"name": "systemd.service.j2", "description": "Systemd unit file"},
        {"name": "docker-compose.yml.j2", "description": "Docker Compose stack"},
    ]


@router.post("/templates/{name}")
async def render_template(
    name: str,
    variables: dict,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """渲染模板预览"""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader("/etc/aegis/templates"),
        autoescape=select_autoescape(),
    )
    try:
        template = env.get_template(name)
        rendered = template.render(**variables)
        return {"rendered": rendered}
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Template error: {e}")