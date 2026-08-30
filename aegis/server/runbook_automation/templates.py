"""Runbook Automation — Templates and execution engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


class StepType(StrEnum):
    SHELL = "shell"                    # 执行 shell 命令
    HTTP = "http"                      # HTTP 请求
    DOCKER = "docker"                  # Docker 操作
    KUBERNETES = "kubernetes"          # K8s 操作
    WAIT = "wait"                      # 等待条件
    APPROVAL = "approval"              # 等待人工审批
    NOTIFY = "notify"                  # 发送通知
    CONDITION = "condition"            # 条件分支


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class RunbookStep:
    id: str
    type: StepType
    name: str
    # 类型相关配置
    command: str | None = None          # shell 命令
    url: str | None = None              # HTTP URL
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    docker_action: str | None = None    # start/stop/restart/remove
    container: str | None = None
    wait_for: str | None = None         # 等待条件表达式
    timeout_seconds: int = 300
    retry: int = 0
    # 条件/分支
    condition: str | None = None
    on_success: list[str] = field(default_factory=list)  # 下一步 ID
    on_failure: list[str] = field(default_factory=list)
    # 审批
    approvers: list[UUID] = field(default_factory=list)
    # 通知
    message: str | None = None


@dataclass
class RunbookTemplate:
    id: UUID
    org_id: UUID
    name: str
    description: str
    trigger_conditions: dict[str, Any]  # 触发条件 JSON
    steps: list[RunbookStep] = field(default_factory=list)
    auto_execute: bool = False
    approval_required: bool = True
    status: str = "active"  # active / inactive / deprecated
    created_by: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def matches_alert(self, alert: dict) -> bool:
        """检查告警是否匹配触发条件"""
        for key, expected in self.trigger_conditions.items():
            if key == "severity" and alert.get("severity") not in expected:
                return False
            if key == "alert_names" and alert.get("name") not in expected:
                return False
            if key == "labels":
                alert_labels = alert.get("labels", {})
                for k, v in expected.items():
                    if alert_labels.get(k) != v:
                        return False
            if key == "service" and alert.get("service") not in expected:
                return False
        return True

    def get_next_steps(self, current_step_id: str, success: bool) -> list[str]:
        """根据当前步骤结果获取下一步"""
        step = next((s for s in self.steps if s.id == current_step_id), None)
        if not step:
            return []
        return step.on_success if success else step.on_failure