"""GitOps Config Sync — Declarative desired state reconciliation from Git.

AEGIS_DESIGN v1.2.0 §10
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.config_management import ConfigExecutor, DesiredState
from aegis.server.persistence import record_audit, get_pool

log = logging.getLogger(__name__)


class SyncStatus(StrEnum):
    PENDING = "pending"
    SYNCING = "syncing"
    SYNCED = "synced"
    FAILED = "failed"
    DRIFTED = "drifted"


class SyncMode(StrEnum):
    MANUAL = "manual"  # 需人工审批
    AUTO = "auto"      # 自动应用
    DRYRUN = "dryrun"  # 仅检测漂移


@dataclass
class GitRepo:
    url: str
    branch: str = "main"
    path_prefix: str = ""  # sub-path within repo
    auth: dict[str, Any] | None = None  # token / ssh key


@dataclass
class SyncResult:
    status: SyncStatus
    drift_detected: bool
    applied_diffs: list[dict] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


class ConfigSyncController:
    """GitOps 调和控制器

    比较 Git 仓库中的期望状态与系统当前状态：
    - 有漂移 → 生成 diff
    - dryrun 模式 → 仅报告
    - auto 模式 → 自动应用
    - manual 模式 → 创建 ChangeRequest 待审批
    """

    def __init__(self, conn: asyncpg.Connection, org_id: UUID):
        self.conn = conn
        self.org_id = org_id
        self.executor = ConfigExecutor(conn)

    async def register_repo(
        self,
        *,
        repo_url: str,
        branch: str = "main",
        path_prefix: str = "",
        mode: SyncMode = SyncMode.MANUAL,
        target_project: UUID | None = None,
    ) -> UUID:
        """注册 Git 仓库用于配置同步"""
        sync_id = uuid.uuid4()
        await self.conn.execute("""
            INSERT INTO config_sync_repos
            (id, org_id, repo_url, branch, path_prefix, mode, target_project, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
            sync_id,
            self.org_id,
            repo_url,
            branch,
            path_prefix,
            mode.value,
            target_project,
            SyncStatus.PENDING.value,
            datetime.now(UTC),
        )
        return sync_id

    async def sync_once(self, sync_id: UUID, actor_user_id: UUID | None = None) -> SyncResult:
        """执行一次同步检查 + 调和"""
        start = datetime.now(UTC)
        repo = await self.conn.fetchrow("""
            SELECT * FROM config_sync_repos WHERE id = $1 AND org_id = $2
        """, sync_id, self.org_id)
        if not repo:
            return SyncResult(SyncStatus.FAILED, False, error="repo not found")

        mode = SyncMode(repo["mode"])

        # 更新状态为 syncing
        await self.conn.execute(
            "UPDATE config_sync_repos SET status = $2, last_sync_at = $3 WHERE id = $1",
            sync_id, SyncStatus.SYNCING.value, start,
        )

        try:
            # 1. Fetch desired state from Git
            desired = await self._fetch_from_git(repo)

            # 2. Compute diff
            diffs = await self.executor.compute_diff(desired)
            drift_detected = any(d.action != "noop" for d in diffs)

            # 3. Apply based on mode
            if not drift_detected:
                await self._update_sync_status(sync_id, SyncStatus.SYNCED, start)
                return SyncResult(SyncStatus.SYNCED, False, duration_ms=_ms(start))

            if mode == SyncMode.DRYRUN:
                await self._update_sync_status(sync_id, SyncStatus.DRIFTED, start)
                return SyncResult(SyncStatus.DRIFTED, True, duration_ms=_ms(start))

            if mode == SyncMode.AUTO:
                result = await self.executor.apply(desired, dry_run=False, actor_user_id=actor_user_id)
                status = SyncStatus.SYNCED if result.success else SyncStatus.FAILED
                await self._update_sync_status(sync_id, status, start, error=result.error)
                # 审计
                await record_audit(
                    self.conn, org_id=self.org_id,
                    action="config.gitops_auto_applied", actor_user_id=actor_user_id,
                    target_type="config_sync", target_id=str(sync_id),
                    metadata={"diffs": len(diffs), "success": result.success},
                )
                return SyncResult(status, True, applied_diffs=[d.__dict__ for d in result.applied], duration_ms=_ms(start))

            # MANUAL → 创建 ChangeRequest
            if mode == SyncMode.MANUAL:
                cr_id = await self._create_change_request_from_drift(sync_id, desired, actor_user_id)
                await self._update_sync_status(sync_id, SyncStatus.PENDING, start)
                return SyncResult(SyncStatus.PENDING, True, duration_ms=_ms(start))

        except Exception as e:
            log.error("config_sync_failed sync_id=%s err=%s", sync_id, e)
            await self._update_sync_status(sync_id, SyncStatus.FAILED, start, error=str(e))
            return SyncResult(SyncStatus.FAILED, False, error=str(e))

    async def _fetch_from_git(self, repo: dict) -> DesiredState:
        """从 Git 仓库拉取期望状态

        支持多种格式:
        - YAML/JSON 清单文件
        - 目录结构约定 (packages/ services/ files/ 等)
        """
        # TODO: 实际 Git 克隆/API 调用
        # 简化：从 DB 的 config_snapshots 获取（模拟 Git 内容）
        snapshot = await self.conn.fetchrow("""
            SELECT desired_state FROM config_snapshots
            WHERE org_id = $1 ORDER BY created_at DESC LIMIT 1
        """, self.org_id)
        if snapshot:
            return DesiredState(**snapshot["desired_state"])
        return DesiredState()

    async def _create_change_request_from_drift(
        self, sync_id: UUID, desired: DesiredState, actor_user_id: UUID | None,
    ) -> UUID:
        """为漂移创建审批型变更请求"""
        cr_id = uuid.uuid4()
        await self.conn.execute("""
            INSERT INTO change_requests
            (id, title, description, status, created_by, actions, created_at, updated_at,
             metadata)
            VALUES ($1, $2, $3, 'pending_approval', $4, $5, $6, $7, $8)
        """,
            cr_id,
            f"GitOps drift sync: {sync_id}",
            f"Auto-generated from config sync repo {sync_id}",
            actor_user_id,
            [{"type": "config.apply", "payload": {"desired": desired.__dict__}}],
            datetime.now(UTC), datetime.now(UTC),
            {"sync_id": str(sync_id), "source": "gitops"},
        )
        return cr_id

    async def _update_sync_status(
        self, sync_id: UUID, status: SyncStatus, start: datetime, error: str | None = None,
    ) -> None:
        await self.conn.execute("""
            UPDATE config_sync_repos
            SET status = $2, last_sync_at = $3, last_error = $4, updated_at = $3
            WHERE id = $1
        """, sync_id, status.value, start, error)


def _ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)


class ConfigSyncScheduler:
    """后台调度器：周期性执行所有注册的同步任务"""

    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, interval_seconds: int = 300) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval_seconds))
        log.info("config_sync_scheduler_started interval=%s", interval_seconds)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("config_sync_scheduler_stopped")

    async def _loop(self, interval_seconds: int) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                log.error("config_sync_tick_failed: %s", e)
            await asyncio.sleep(interval_seconds)

    async def _tick(self) -> None:
        """遍历所有注册的 repo 执行同步"""
        pool = get_pool()
        async with pool.acquire() as conn:
            repos = await conn.fetch("SELECT id, org_id FROM config_sync_repos")
            for repo in repos:
                controller = ConfigSyncController(conn, repo["org_id"])
                await controller.sync_once(repo["id"])


# 全局调度器实例
scheduler = ConfigSyncScheduler()
