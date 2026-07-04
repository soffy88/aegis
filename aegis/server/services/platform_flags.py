"""运行时平台开关 —— DB 支撑、事中可翻不必重启 (DESIGN §5.3).

aegis_platform_flags(key, enabled, reason)。缺行=未启用(默认放行)。
全局急停 key=AUTOHEAL_KILL_SWITCH:enabled=TRUE 时编排层停止一切自愈动作。
"""

from __future__ import annotations

from typing import Any

# 全局自愈急停开关的 flag key
AUTOHEAL_KILL_SWITCH = "autoheal"


async def is_flag_enabled(conn: Any, key: str) -> bool:
    """flag 是否置位。缺行 → False(默认放行)。查询失败向上抛,由调用方兜底。"""
    row = await conn.fetchrow("SELECT enabled FROM aegis_platform_flags WHERE key = $1", key)
    return bool(row and row["enabled"])


async def set_flag(conn: Any, key: str, *, enabled: bool, reason: str | None = None) -> None:
    """置位/清除 flag(upsert)。运维事中经 API/SQL 调用以急停或恢复。"""
    await conn.execute(
        """
        INSERT INTO aegis_platform_flags (key, enabled, reason, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (key) DO UPDATE
            SET enabled = EXCLUDED.enabled, reason = EXCLUDED.reason, updated_at = now()
        """,
        key,
        enabled,
        reason,
    )
