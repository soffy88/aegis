"""Configuration Management — Idempotent executor with dry-run + diff preview.

AEGIS_DESIGN v1.1.0 §7
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.config_management.models import (
    DesiredState,
    DiffEntry,
    ExecutionResult,
    FileSpec,
    PackageSpec,
    ServiceSpec,
    SysctlSpec,
    UserSpec,
)

log = logging.getLogger(__name__)


class ConfigExecutor:
    """声明式配置执行器，支持干运行、差异预览、原子应用 + 回滚点"""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn
        self._facts_cache: dict[str, Any] = {}

    async def compute_diff(self, desired: DesiredState) -> list[DiffEntry]:
        """计算当前状态与期望状态的差异（不修改系统）"""
        await self._gather_facts()
        diffs = []

        # Packages
        for pkg in desired.packages:
            diffs.extend(await self._diff_package(pkg))

        # Services
        for svc in desired.services:
            diffs.extend(await self._diff_service(svc))

        # Files
        for f in desired.files:
            diffs.extend(await self._diff_file(f))

        # Sysctl
        for s in desired.sysctl:
            diffs.extend(await self._diff_sysctl(s))

        # Users
        for u in desired.users:
            diffs.extend(await self._diff_user(u))

        return diffs

    async def apply(
        self,
        desired: DesiredState,
        dry_run: bool = True,
        actor_user_id: UUID | None = None,
    ) -> ExecutionResult:
        """执行配置应用

        dry_run=True: 仅返回 diff，不修改系统
        dry_run=False: 原子性应用所有变更，失败时尝试回滚
        """
        start = time.monotonic()
        diffs = await self.compute_diff(desired)

        # 过滤出需要变更的项
        changes = [d for d in diffs if d.action != "noop"]

        result = ExecutionResult(
            success=True,
            diffs=diffs,
            applied=[],
            failed=[],
            rollback_available=not dry_run and len(changes) > 0,
        )

        if dry_run:
            result.duration_ms = int((time.monotonic() - start) * 1000)
            return result

        # 执行变更
        rollback_points: list[dict[str, Any]] = []
        for diff in changes:
            try:
                await self._apply_diff(diff, rollback_points)
                result.applied.append(diff)
            except Exception as e:
                log.error("config_apply_failed diff=%s err=%s", diff, e)
                result.success = False
                result.error = str(e)
                result.failed.append(diff)

                # 尝试回滚已应用的变更
                if rollback_points:
                    await self._rollback(rollback_points)
                break

        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    # ── 事实收集 ────────────────────────────────────────────────────

    async def _gather_facts(self) -> None:
        """收集当前系统事实（包、服务、文件、sysctl、用户）"""
        if self._facts_cache:
            return

        # Packages
        try:
            rows = await self.conn.fetch("""
                SELECT name, version FROM installed_packages
            """)
            self._facts_cache["packages"] = {r["name"]: r["version"] for r in rows}
        except Exception:
            self._facts_cache["packages"] = {}

        # Services (systemd)
        try:
            rows = await self.conn.fetch("""
                SELECT name, active_state, sub_state, enabled
                FROM systemd_services
            """)
            self._facts_cache["services"] = {
                r["name"]: {"active": r["active_state"], "enabled": r["enabled"]}
                for r in rows
            }
        except Exception:
            self._facts_cache["services"] = {}

        # Sysctl
        try:
            rows = await self.conn.fetch("SELECT key, value FROM sysctl_current")
            self._facts_cache["sysctl"] = {r["key"]: r["value"] for r in rows}
        except Exception:
            self._facts_cache["sysctl"] = {}

        # Users
        try:
            rows = await self.conn.fetch("""
                SELECT username, groups, shell, home
                FROM system_users
            """)
            self._facts_cache["users"] = {
                r["username"]: dict(r) for r in rows
            }
        except Exception:
            self._facts_cache["users"] = {}

    # ── Diff 计算 ──────────────────────────────────────────────────

    async def _diff_package(self, pkg: PackageSpec) -> list[DiffEntry]:
        current_version = self._facts_cache["packages"].get(pkg.name)
        if pkg.state == PackageSpec.PRESENT:
            if current_version is None:
                return [DiffEntry("package", pkg.name, "create", None,
                                 {"name": pkg.name, "version": pkg.version},
                                 {"version": (None, pkg.version)})]
            elif pkg.version and current_version != pkg.version:
                return [DiffEntry("package", pkg.name, "update",
                                 {"version": current_version},
                                 {"name": pkg.name, "version": pkg.version},
                                 {"version": (current_version, pkg.version)})]
            return [DiffEntry("package", pkg.name, "noop",
                             {"version": current_version},
                             {"name": pkg.name, "version": pkg.version}, {})]
        elif pkg.state == PackageSpec.ABSENT:
            if current_version is not None:
                return [DiffEntry("package", pkg.name, "delete",
                                 {"version": current_version},
                                 {"name": pkg.name, "state": "absent"},
                                 {"version": (current_version, None)})]
            return [DiffEntry("package", pkg.name, "noop",
                             None, {"name": pkg.name, "state": "absent"}, {})]
        return []

    async def _diff_service(self, svc: ServiceSpec) -> list[DiffEntry]:
        current = self._facts_cache["services"].get(svc.name)
        desired_state = svc.state.value

        if current is None:
            return [DiffEntry("service", svc.name, "create", None,
                             {"name": svc.name, "state": desired_state, "enabled": svc.enabled},
                             {"state": (None, desired_state)})]

        changes = {}
        if current["active"] != desired_state:
            changes["active"] = (current["active"], desired_state)
        if svc.enabled is not None and current.get("enabled") != svc.enabled:
            changes["enabled"] = (current.get("enabled"), svc.enabled)

        if changes:
            return [DiffEntry("service", svc.name, "update", current,
                             {"name": svc.name, "state": desired_state, "enabled": svc.enabled},
                             changes)]
        return [DiffEntry("service", svc.name, "noop", current,
                         {"name": svc.name, "state": desired_state}, {})]

    async def _diff_file(self, f: FileSpec) -> list[DiffEntry]:
        # 文件 diff 需要实际读取文件内容
        # 这里简化：计算期望内容的 hash
        desired_content = f.content or f"template:{f.template}"
        desired_hash = hashlib.sha256(desired_content.encode()).hexdigest()[:16]

        current_hash = None
        try:
            import aiofiles
            async with aiofiles.open(f.path, "rb") as fp:
                content = await fp.read()
                current_hash = hashlib.sha256(content).hexdigest()[:16]
        except FileNotFoundError:
            current_hash = None
        except Exception:
            pass

        if current_hash is None:
            return [DiffEntry("file", f.path, "create", None,
                             {"path": f.path, "hash": desired_hash, "mode": f.mode},
                             {"hash": (None, desired_hash)})]
        elif current_hash != desired_hash:
            return [DiffEntry("file", f.path, "update",
                             {"hash": current_hash},
                             {"path": f.path, "hash": desired_hash, "mode": f.mode},
                             {"hash": (current_hash, desired_hash)})]
        return [DiffEntry("file", f.path, "noop",
                         {"hash": current_hash},
                         {"path": f.path, "hash": desired_hash}, {})]

    async def _diff_sysctl(self, s: SysctlSpec) -> list[DiffEntry]:
        current = self._facts_cache["sysctl"].get(s.key)
        desired = str(s.value)
        if current != desired:
            return [DiffEntry("sysctl", s.key, "update",
                             {"value": current}, {"key": s.key, "value": desired},
                             {"value": (current, desired)})]
        return [DiffEntry("sysctl", s.key, "noop",
                         {"value": current}, {"key": s.key, "value": desired}, {})]

    async def _diff_user(self, u: UserSpec) -> list[DiffEntry]:
        current = self._facts_cache["users"].get(u.name)
        if u.state == PackageSpec.PRESENT:
            if current is None:
                return [DiffEntry("user", u.name, "create", None,
                                 {"name": u.name, "groups": u.groups, "shell": u.shell},
                                 {"name": (None, u.name)})]
            # 检查 groups/shell 变更
            changes = {}
            if set(current.get("groups", [])) != set(u.groups):
                changes["groups"] = (current.get("groups", []), u.groups)
            if current.get("shell") != u.shell:
                changes["shell"] = (current.get("shell"), u.shell)
            if changes:
                return [DiffEntry("user", u.name, "update", current,
                                 {"name": u.name, "groups": u.groups, "shell": u.shell},
                                 changes)]
            return [DiffEntry("user", u.name, "noop", current,
                             {"name": u.name, "groups": u.groups}, {})]
        elif u.state == PackageSpec.ABSENT and current is not None:
            return [DiffEntry("user", u.name, "delete", current,
                             {"name": u.name, "state": "absent"},
                             {"name": (u.name, None)})]
        return []

    # ── 应用变更 ──────────────────────────────────────────────────

    async def _apply_diff(self, diff: DiffEntry, rollback_points: list[dict]) -> None:
        """应用单个 diff，记录回滚点"""
        # 记录回滚点
        rollback_points.append({
            "type": diff.resource_type,
            "name": diff.resource_name,
            "previous": diff.current,
        })

        # 实际执行（通过 SSH/agent 或本地执行器）
        # 这里框架化，实际执行由具体模块实现
        await self._execute_action(diff)

    async def _execute_action(self, diff: DiffEntry) -> None:
        """执行具体动作 - 由子类或插件实现"""
        # TODO: 实现具体执行逻辑 (apt install, systemctl, file write 等)
        # 可通过 oprim / agent 执行
        pass

    async def _rollback(self, rollback_points: list[dict]) -> None:
        """回滚已应用的变更"""
        for point in reversed(rollback_points):
            try:
                # 反向操作
                await self._execute_rollback(point)
            except Exception as e:
                log.error("rollback_failed point=%s err=%s", point, e)

    async def _execute_rollback(self, point: dict) -> None:
        """执行回滚 - 由具体实现"""
        pass


# ── 便捷函数 ───────────────────────────────────────────────────────

async def dry_run_config(
    conn: asyncpg.Connection,
    desired: DesiredState,
) -> ExecutionResult:
    """便捷函数：仅计算 diff"""
    executor = ConfigExecutor(conn)
    diffs = await executor.compute_diff(desired)
    return ExecutionResult(
        success=True,
        diffs=diffs,
        applied=[],
        failed=[],
        rollback_available=False,
    )


async def apply_config(
    conn: asyncpg.Connection,
    desired: DesiredState,
    actor_user_id: UUID | None = None,
) -> ExecutionResult:
    """便捷函数：执行应用"""
    executor = ConfigExecutor(conn)
    return await executor.apply(desired, dry_run=False, actor_user_id=actor_user_id)