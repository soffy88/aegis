"""Multi-Tenancy — Resource quota management and enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

log = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """Raised when a resource request exceeds tenant quota."""
    def __init__(self, resource: str, requested: int, limit: int, current: int):
        self.resource = resource
        self.requested = requested
        self.limit = limit
        self.current = current
        super().__init__(
            f"Quota exceeded for {resource}: requested {requested}, "
            f"current {current}, limit {limit}"
        )


@dataclass
class Quota:
    resource: str
    limit: int
    current: int = 0


RESOURCE_TYPES = frozenset({
    "apps",           # 最大应用数
    "containers",     # 最大容器数
    "cpu_cores",      # CPU 核心数
    "memory_gb",      # 内存 GB
    "disk_gb",        # 磁盘 GB
    "network_mbps",   # 网络带宽 Mbps
})


class QuotaManager:
    """租户资源配额管理器"""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def get_quota(self, org_id: UUID, resource: str) -> Quota | None:
        """获取配额限制"""
        row = await self.conn.fetchrow(
            "SELECT limit FROM tenant_quotas WHERE org_id = $1 AND resource = $2",
            org_id,
            resource,
        )
        if not row:
            return None
        return Quota(resource=resource, limit=row["limit"])

    async def set_quota(
        self,
        org_id: UUID,
        resource: str,
        limit: int,
    ) -> Quota:
        """设置/更新配额 (需 owner 权限)"""
        if resource not in RESOURCE_TYPES:
            raise ValueError(f"Unknown resource type: {resource}")

        await self.conn.execute(
            """
            INSERT INTO tenant_quotas (org_id, resource, limit)
            VALUES ($1, $2, $3)
            ON CONFLICT (org_id, resource) DO UPDATE SET limit = EXCLUDED.limit
            """,
            org_id,
            resource,
            limit,
        )
        return Quota(resource=resource, limit=limit)

    async def list_quotas(self, org_id: UUID) -> list[Quota]:
        """列出所有配额"""
        rows = await self.conn.fetch(
            "SELECT resource, limit FROM tenant_quotas WHERE org_id = $1 ORDER BY resource",
            org_id,
        )
        return [Quota(resource=r["resource"], limit=r["limit"]) for r in rows]

    async def get_current_usage(self, org_id: UUID) -> dict[str, int]:
        """计算当前实际使用量"""
        usage = {}

        # Apps
        apps_count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM installed_apps WHERE org_id = $1 AND status != 'uninstalled'",
            org_id,
        )
        usage["apps"] = apps_count or 0

        # Containers (通过 Docker API 或 agent 统计)
        # 简化：统计 installed_apps 中 running 状态
        containers_count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM installed_apps WHERE org_id = $1 AND status = 'running'",
            org_id,
        )
        usage["containers"] = containers_count or 0

        # CPU/Memory/Disk - 需要从 metrics 聚合
        # TODO: 从 agent_metrics 聚合
        usage["cpu_cores"] = 0
        usage["memory_gb"] = 0
        usage["disk_gb"] = 0
        usage["network_mbps"] = 0

        return usage

    async def check_quota(
        self,
        org_id: UUID,
        resource: str,
        requested: int = 1,
    ) -> tuple[bool, Quota | None, int]:
        """检查是否超额

        Returns: (allowed, quota, current_usage)
        """
        quota = await self.get_quota(org_id, resource)
        if not quota:
            return True, None, 0  # 无限制

        usage = await self.get_current_usage(org_id)
        current = usage.get(resource, 0)

        if current + requested > quota.limit:
            return False, quota, current

        return True, quota, current

    async def enforce_quota(
        self,
        org_id: UUID,
        resource: str,
        requested: int = 1,
    ) -> None:
        """强制执行配额检查，超额抛出异常"""
        allowed, quota, current = await self.check_quota(org_id, resource, requested)
        if not allowed:
            raise QuotaExceededError(resource, requested, quota.limit, current)

    async def reserve_quota(
        self,
        org_id: UUID,
        resource: str,
        requested: int = 1,
    ) -> bool:
        """尝试预留配额 (乐观锁，实际扣减在业务完成时)"""
        allowed, _, _ = await self.check_quota(org_id, resource, requested)
        return allowed

    async def release_quota(
        self,
        org_id: UUID,
        resource: str,
        released: int = 1,
    ) -> None:
        """释放配额 (用于回滚/删除操作)"""
        # 配额基于实际使用量计算，无需显式释放
        # 这里可记录审计日志
        pass


# ── 便捷装饰器 ──────────────────────────────────────────────────────────

def enforce_quota(resource: str, requested: int = 1):
    """FastAPI 依赖：强制执行配额"""
    from functools import wraps
    from fastapi import Depends, HTTPException, status

    async def dependency(
        conn: asyncpg.Connection = Depends(get_pool().acquire),
        user: Any = Depends(get_current_user),  # 实际应从上下文获取
    ) -> None:
        if not user or not user.orgs:
            return
        org_id = user.orgs[0].org_id
        mgr = QuotaManager(conn)
        await mgr.enforce_quota(org_id, resource, requested)

    return Depends(dependency)