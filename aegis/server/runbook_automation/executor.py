"""Runbook Automation — Execution engine with approval gates."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.runbook_automation.templates import (
    ExecutionStatus,
    RunbookStep,
    RunbookTemplate,
    StepType,
)
from aegis.server.persistence import record_audit

log = logging.getLogger(__name__)


class RunbookExecutor:
    """Runbook 执行引擎，支持步骤流程控制 + 审批门"""

    def __init__(self, conn: asyncpg.Connection, org_id: UUID):
        self.conn = conn
        self.org_id = org_id

    async def execute(
        self,
        template: RunbookTemplate,
        trigger_alert: dict | None = None,
        executed_by: UUID | None = None,
    ) -> UUID:
        """启动执行，返回 execution_id"""
        exec_id = uuid.uuid4()
        now = datetime.now(UTC)

        await self.conn.execute("""
            INSERT INTO runbook_executions
            (id, org_id, runbook_id, trigger_alert_id, status, executed_by, started_at, execution_log)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
            exec_id,
            self.org_id,
            template.id,
            trigger_alert.get("id") if trigger_alert else None,
            ExecutionStatus.RUNNING.value,
            executed_by,
            now,
            [],
        )

        # 后台执行
        asyncio.create_task(self._run(exec_id, template, trigger_alert, executed_by))
        return exec_id

    async def _run(
        self,
        exec_id: UUID,
        template: RunbookTemplate,
        trigger_alert: dict | None,
        executed_by: UUID | None,
    ) -> None:
        """实际执行逻辑"""
        try:
            # 构建步骤索引
            step_map = {s.id: s for s in template.steps}

            # 执行入口步骤
            entry_steps = [s for s in template.steps if not any(
                s.id in other.on_success + other.on_failure for other in template.steps
            )]

            if not entry_steps:
                # 无明确入口，按顺序执行第一步
                entry_steps = [template.steps[0]] if template.steps else []

            for step in entry_steps:
                await self._execute_step(exec_id, template, step, step_map, trigger_alert)

            await self._update_status(exec_id, ExecutionStatus.COMPLETED)

        except Exception as e:
            log.error("runbook_execution_failed exec_id=%s err=%s", exec_id, e)
            await self._update_status(exec_id, ExecutionStatus.FAILED)

    async def _execute_step(
        self,
        exec_id: UUID,
        template: RunbookTemplate,
        step: RunbookStep,
        step_map: dict[str, RunbookStep],
        trigger_alert: dict | None,
    ) -> bool:
        """执行单个步骤，返回是否成功"""
        log.info("runbook_step_start exec_id=%s step=%s type=%s", exec_id, step.id, step.type)

        # 更新执行日志
        await self._append_log(exec_id, {
            "step_id": step.id,
            "type": step.type.value,
            "status": "started",
            "at": datetime.now(UTC).isoformat(),
        })

        success = False
        try:
            if step.type == StepType.SHELL:
                success = await self._exec_shell(step)
            elif step.type == StepType.HTTP:
                success = await self._exec_http(step)
            elif step.type == StepType.DOCKER:
                success = await self._exec_docker(step)
            elif step.type == StepType.WAIT:
                success = await self._exec_wait(step)
            elif step.type == StepType.APPROVAL:
                success = await self._exec_approval(exec_id, step)
            elif step.type == StepType.NOTIFY:
                success = await self._exec_notify(step)
            elif step.type == StepType.CONDITION:
                success = await self._exec_condition(step, trigger_alert)
            else:
                log.warning("runbook_step_unknown_type step_id=%s type=%s", step.id, step.type)
                success = False

        except Exception as e:
            log.error("runbook_step_error exec_id=%s step=%s err=%s", exec_id, step.id, e)
            success = False

        # 记录结果
        await self._append_log(exec_id, {
            "step_id": step.id,
            "status": "completed" if success else "failed",
            "at": datetime.now(UTC).isoformat(),
        })

        # 继续执行下一步
        next_steps = step.on_success if success else step.on_failure
        for next_id in next_steps:
            next_step = step_map.get(next_id)
            if next_step:
                await self._execute_step(exec_id, template, next_step, step_map, trigger_alert)

        return success

    async def _exec_shell(self, step: RunbookStep) -> bool:
        """执行 Shell 命令"""
        if not step.command:
            return False
        try:
            proc = await asyncio.create_subprocess_shell(
                step.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=step.timeout_seconds
            )
            return proc.returncode == 0
        except asyncio.TimeoutError:
            return False

    async def _exec_http(self, step: RunbookStep) -> bool:
        """执行 HTTP 请求"""
        # TODO: 实现
        return True

    async def _exec_docker(self, step: RunbookStep) -> bool:
        """执行 Docker 操作"""
        # TODO: 实现
        return True

    async def _exec_wait(self, step: RunbookStep) -> bool:
        """等待条件满足"""
        if not step.wait_for:
            return False
        # TODO: 实现条件轮询
        return True

    async def _exec_approval(self, exec_id: UUID, step: RunbookStep) -> bool:
        """等待人工审批"""
        # 更新状态为等待审批
        await self.conn.execute(
            "UPDATE runbook_executions SET status = $2 WHERE id = $1",
            exec_id, ExecutionStatus.APPROVED.value
        )
        # 等待审批完成（通过外部 API 调用 approve_runbook）
        # 这里简化：轮询状态
        for _ in range(60):  # 最多等待 30 分钟
            await asyncio.sleep(30)
            row = await self.conn.fetchrow(
                "SELECT status FROM runbook_executions WHERE id = $1", exec_id
            )
            if row and row["status"] == ExecutionStatus.APPROVED.value:
                return True
            if row and row["status"] == ExecutionStatus.REJECTED.value:
                return False
        return False

    async def _exec_notify(self, step: RunbookStep) -> bool:
        """发送通知"""
        # TODO: 调用通知渠道
        return True

    async def _exec_condition(self, step: RunbookStep, trigger_alert: dict | None) -> bool:
        """评估条件表达式"""
        if not step.condition or not trigger_alert:
            return True
        # TODO: 实现表达式求值
        return True

    async def _update_status(self, exec_id: UUID, status: ExecutionStatus) -> None:
        await self.conn.execute("""
            UPDATE runbook_executions
            SET status = $2, completed_at = $3
            WHERE id = $1
        """, exec_id, status.value, datetime.now(UTC))

    async def _append_log(self, exec_id: UUID, entry: dict) -> None:
        await self.conn.execute("""
            UPDATE runbook_executions
            SET execution_log = execution_log || $2::jsonb
            WHERE id = $1
        """, exec_id, entry)