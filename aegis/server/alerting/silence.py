"""Alert silencing — maintenance windows, manual silences, auto-silence on deploy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


class SilenceType(StrEnum):
    MANUAL = "manual"           # 手动静默
    MAINTENANCE = "maintenance" # 维护窗口
    DEPLOY = "deploy"           # 部署期间
    AUTO = "auto"               # 自动抑制


@dataclass
class Silence:
    id: UUID
    org_id: UUID
    type: SilenceType
    # 匹配条件
    target_type: str | None = None   # "container" / "service" / "metric"
    target_id: str | None = None
    alert_name: str | None = None
    labels: dict[str, str] = None    # label 选择器
    # 时间
    starts_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ends_at: datetime | None = None  # None = 无限期
    # 元数据
    created_by: UUID | None = None
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_active(self) -> bool:
        now = datetime.now(UTC)
        if now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def matches(self, alert: dict) -> bool:
        """检查告警是否匹配此静默规则"""
        if self.target_type and alert.get("target_type") != self.target_type:
            return False
        if self.target_id and alert.get("target_id") != self.target_id:
            return False
        if self.alert_name and alert.get("name") != self.alert_name:
            return False
        if self.labels:
            for k, v in self.labels.items():
                if alert.get("labels", {}).get(k) != v:
                    return False
        return True


class SilenceManager:
    """静默管理器"""

    def __init__(self, conn):
        self.conn = conn

    async def create_silence(
        self,
        org_id: UUID,
        type: SilenceType,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        alert_name: str | None = None,
        labels: dict[str, str] | None = None,
        duration_seconds: int | None = None,
        created_by: UUID | None = None,
        reason: str | None = None,
    ) -> Silence:
        """创建静默"""
        from uuid import uuid4
        silence = Silence(
            id=uuid4(),
            org_id=org_id,
            type=type,
            target_type=target_type,
            target_id=target_id,
            alert_name=alert_name,
            labels=labels,
            ends_at=datetime.now(UTC) + timedelta(seconds=duration_seconds) if duration_seconds else None,
            created_by=created_by,
            reason=reason,
        )
        await self.conn.execute("""
            INSERT INTO alert_silences
            (id, org_id, type, target_type, target_id, alert_name, labels,
             starts_at, ends_at, created_by, reason, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
            silence.id, silence.org_id, silence.type.value,
            silence.target_type, silence.target_id, silence.alert_name,
            json.dumps(silence.labels or {}),
            silence.starts_at, silence.ends_at, silence.created_by,
            silence.reason, silence.created_at,
        )
        return silence

    async def get_active_silences(self, org_id: UUID) -> list[Silence]:
        """获取当前生效的静默"""
        rows = await self.conn.fetch("""
            SELECT * FROM alert_silences
            WHERE org_id = $1
            AND (ends_at IS NULL OR ends_at > NOW())
            AND starts_at <= NOW()
        """, org_id)
        return [self._row_to_silence(r) for r in rows]

    async def check_silenced(self, org_id: UUID, alert: dict) -> bool:
        """检查告警是否被静默"""
        silences = await self.get_active_silences(org_id)
        for s in silences:
            if s.matches(alert):
                return True
        return False

    async def cancel_silence(self, silence_id: UUID) -> bool:
        """取消静默"""
        result = await self.conn.execute(
            "DELETE FROM alert_silences WHERE id = $1", silence_id
        )
        return result != "DELETE 0"

    def _row_to_silence(self, row) -> Silence:
        return Silence(
            id=row["id"],
            org_id=row["org_id"],
            type=SilenceType(row["type"]),
            target_type=row["target_type"],
            target_id=row["target_id"],
            alert_name=row["alert_name"],
            labels=row["labels"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            created_by=row["created_by"],
            reason=row["reason"],
            created_at=row["created_at"],
        )


from datetime import timedelta
import json