"""Alert grouping — correlate alerts via causal chain, suppress noise."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


class GroupingStrategy(StrEnum):
    CAUSAL_CHAIN = "causal_chain"  # 基于 event_trail 因果链路
    SAME_SERVICE = "same_service"  # 同一服务下
    SAME_METRIC = "same_metric"    # 同一指标连续触发
    RATE_LIMIT = "rate_limit"      # 限流抑制


@dataclass
class GroupRule:
    """分组规则配置"""
    name: str
    strategy: GroupingStrategy
    # 根据 strategy 的配置参数
    max_groups: int = 100  # 最大分组数
    suppressive: bool = True   # 是否抑制同组后续告警
    timeout_seconds: int = 300 # 分组超时时间


@dataclass
class AlertGroup:
    """活跃的告警分组"""
    id: UUID
    rule_name: str
    alerts: list[dict] = field(default_factory=list)  # 属于该分组的原始告警
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    alert_count: int = 0
    suppressed_count: int = 0
    status: str = "active"  # active / resolved / suppressed


class AlertGrouper:
    """基于因果链路和服务维度的告警分组"""

    def __init__(self, org_id: UUID, strategy: GroupingStrategy = GroupingStrategy.CAUSAL_CHAIN):
        self.org_id = org_id
        self.strategy = strategy
        self.active_groups: dict[str, AlertGroup] = {}

    async def add_alert(self, alert: dict) -> AlertGroup | None:
        """将新告警加入现有分组或创建新分组"""

        if self.strategy == GroupingStrategy.CAUSAL_CHAIN:
            return await self._group_by_causal_chain(alert)
        elif self.strategy == GroupingStrategy.SAME_SERVICE:
            return await self._group_by_service(alert)
        elif self.strategy == GroupingStrategy.SAME_METRIC:
            return await self._group_by_metric(alert)
        elif self.strategy == GroupingStrategy.RATE_LIMIT:
            return await self._group_by_rate_limit(alert)

        return None

    async def _group_by_causal_chain(self, alert: dict) -> AlertGroup | None:
        """基于因果链路分组：同一事件源的后续告警归入同一组"""
        alert_id = alert.get("id")
        alert_type = alert.get("type")
        target_type = alert.get("target_type")
        target_id = alert.get("target_id")

        if not all([alert_id, target_type, target_id]):
            # 缺少关键信息，创建独立分组
            return await self._create_new_group(alert)

        # 查找相关的历史事件
        # TODO: 查询 event_trail 找到因果链路
        # 简化逻辑：检查是否有相同 target_type+target_id 的近期告警
        key = f"{target_type}:{target_id}"

        # 检查是否已有活跃分组
        for g in self.active_groups.values():
            if any(a.get("target_id") == target_id for a in g.alerts):
                # 加入现有分组
                g.alerts.append(alert)
                g.alert_count += 1
                g.last_updated = datetime.now(UTC)
                return g

        # 创建新分组
        return await self._create_new_group(alert)

    async def _group_by_service(self, alert: dict) -> AlertGroup | None:
        """按服务维度分组"""
        service = alert.get("target_type") or alert.get("service")
        if not service:
            return await self._create_new_group(alert)

        key = f"service:{service}"
        for g in self.active_groups.values():
            if any(a.get("service") == service for a in g.alerts):
                g.alerts.append(alert)
                g.alert_count += 1
                g.last_updated = datetime.now(UTC)
                return g

        return await self._create_new_group(alert)

    async def _group_by_metric(self, alert: dict) -> AlertGroup | None:
        """按指标维度分组（连续触发阈值）"""
        metric = alert.get("metric_name") or alert.get("metric")
        if not metric:
            return await self._create_new_group(alert)

        key = f"metric:{metric}"
        for g in self.active_groups.values():
            if any(a.get("metric") == metric for a in g.alerts):
                # 只有在短时间内连续触发才合并
                last = g.alerts[-1] if g.alerts else {}
                if last.get("ts") and (datetime.now(UTC).timestamp() - last.get("ts", 0)) < 300:
                    g.alerts.append(alert)
                    g.alert_count += 1
                    g.last_updated = datetime.now(UTC)
                    return g

        return await self._create_new_group(alert)

    async def _group_by_rate_limit(self, alert: dict) -> AlertGroup | None:
        """速率限制分组：抑制短时间内的大量重复告警"""
        # 简化实现：始终创建新分组但标记为抑制
        return await self._create_new_group(alert, suppressive=True)

    async def _create_new_group(self, alert: dict, suppressive: bool = False) -> AlertGroup:
        """创建新分组"""
        from uuid import uuid4
        from datetime import UTC

        g_id = uuid4()
        g = AlertGroup(
            id=g_id,
            alerts=[alert],
            alert_count=1,
            suppressed_count=0,
            status="active" if not suppressive else "suppressed",
        )
        self.active_groups[str(g_id)] = g
        return g

    async def resolve_group(self, group_id: str) -> AlertGroup | None:
        """标记分组为已解决"""
        if group_id in self.active_groups:
            g = self.active_groups[group_id]
            g.status = "resolved"
            g.last_updated = datetime.now(UTC)
            return g
        return None

    async def get_active_alerts(self) -> list[dict]:
        """返回所有活跃分组中未抑制的告警"""
        result = []
        for g in self.active_groups.values():
            if g.status == "active":
                result.extend(g.alerts)
        return result