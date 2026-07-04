"""§9/§3.3 变更冻结窗口 —— 高风险时段禁自动变更.

冻结窗口内 MUST 禁止部署与自动自愈(DESIGN §3.3:"S2 磁盘压测撞行情剧烈时段不可接受")。
窗口活跃由 oprim.window_active_check 判定(支持 none/daily/weekly)。配置非法或未设 → 不冻结
(fail-open:不因坏配置瘫痪自愈;冻结是额外安全层,缺失退回常规闸门)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def _parse_weekdays(raw: str) -> list[int] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip() != ""]
    except ValueError:
        return None


def is_change_frozen(cfg: Any, now: datetime) -> bool:
    """当前是否落在活跃变更冻结窗口内。未配置/配置非法 → False(fail-open)。"""
    if not cfg.change_freeze_start or cfg.change_freeze_duration_seconds <= 0:
        return False
    from oprim import window_active_check  # noqa: PLC0415

    try:
        start = datetime.fromisoformat(cfg.change_freeze_start)
        status = window_active_check(
            now=now,
            start=start,
            duration_seconds=int(cfg.change_freeze_duration_seconds),
            recurrence=cfg.change_freeze_recurrence,
            weekdays=_parse_weekdays(cfg.change_freeze_weekdays),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("change_freeze_config_error err=%s (fail-open,不冻结)", exc)
        return False
    return bool(status.active)
