"""Notification routing — route alerts to channels based on rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


class ChannelType(StrEnum):
    WEBHOOK = "webhook"
    EMAIL = "email"
    SLACK = "slack"
    PAGERDUTY = "pagerduty"
    TELEGRAM = "telegram"


class MatchType(StrEnum):
    ALL = "all"
    ANY = "any"
    NONE = "none"


@dataclass
class RouteRule:
    id: UUID
    name: str
    org_id: UUID
    # 匹配条件
    match_type: MatchType = MatchType.ALL
    severity: list[str] | None = None      # ["critical", "warning"]
    alert_names: list[str] | None = None
    labels: dict[str, str] | None = None
    # 动作
    channels: list[ChannelType] = field(default_factory=list)
    channel_configs: dict[str, dict] = field(default_factory=dict)  # channel -> config
    # 调度
    enabled: bool = True
    repeat_interval: int = 300  # 重复通知间隔 (秒)
    continue_on_match: bool = True  # 继续匹配下一条规则
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class NotificationRouter:
    """告警路由器：根据规则决定发送到哪些渠道"""

    def __init__(self, conn):
        self.conn = conn

    async def route_alert(self, alert: dict) -> list[tuple[ChannelType, dict]]:
        """返回 (channel_type, channel_config) 列表"""
        org_id = alert.get("org_id")
        if not org_id:
            return []

        rules = await self._get_rules(org_id)
        results = []

        for rule in rules:
            if not rule.enabled:
                continue
            if self._matches(rule, alert):
                for ch in rule.channels:
                    config = rule.channel_configs.get(ch.value, {})
                    results.append((ch, config))
                if not rule.continue_on_match:
                    break

        return results

    def _matches(self, rule: RouteRule, alert: dict) -> bool:
        """检查告警是否匹配规则"""
        checks = []

        if rule.severity:
            checks.append(alert.get("severity") in rule.severity)
        if rule.alert_names:
            checks.append(alert.get("name") in rule.alert_names)
        if rule.labels:
            alert_labels = alert.get("labels", {})
            for k, v in rule.labels.items():
                checks.append(alert_labels.get(k) == v)

        if not checks:
            return True

        if rule.match_type == MatchType.ALL:
            return all(checks)
        elif rule.match_type == MatchType.ANY:
            return any(checks)
        elif rule.match_type == MatchType.NONE:
            return not any(checks)
        return False

    async def _get_rules(self, org_id: UUID) -> list[RouteRule]:
        rows = await self.conn.fetch("""
            SELECT * FROM notification_rules
            WHERE org_id = $1 ORDER BY created_at
        """, org_id)
        return [self._row_to_rule(r) for r in rows]

    def _row_to_rule(self, row) -> RouteRule:
        return RouteRule(
            id=row["id"],
            name=row["name"],
            org_id=row["org_id"],
            match_type=MatchType(row["match_type"]),
            severity=row["severity"],
            alert_names=row["alert_names"],
            labels=row["labels"],
            channels=[ChannelType(c) for c in row["channels"]],
            channel_configs=row["channel_configs"],
            enabled=row["enabled"],
            repeat_interval=row["repeat_interval"],
            continue_on_match=row["continue_on_match"],
            created_at=row["created_at"],
        )