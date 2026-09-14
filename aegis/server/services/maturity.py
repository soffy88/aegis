"""§2.1 / C-2.1 成熟度阶梯 + 演练结果 + 自动降级.

成熟度不靠声明,靠演练证明。每个能力有当前 level (L0–L4)。写入一次演练结果后断言:
连续 N 次 (默认 2) 演练失败 MUST 自动降回 L1 —— 重新计为负债,其 auto 模式自动禁用
(I2),直到重新转绿。

提供:
- `record_drill(conn, capability, scenario, passed, detail)` 落库 + 触发降级判定
- `capability_level(conn, capability)` 当前等级查询
- `is_auto_allowed(conn, capability)` 是否允许 auto 模式 (L2+ 且未连续失败且未降级)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_SCENARIOS = ("S1", "S2", "S3", "S4")


async def record_drill(
    conn: Any,
    *,
    capability: str,
    scenario: str,
    passed: bool,
    detail: str | None = None,
    threshold: int = 2,
) -> dict[str, Any]:
    """写入一条演练结果并触发 §2.1 自动降级判定。

    返回该能力降级后的最新状态 (level / auto_enabled / consecutive_failures)。

    降级逻辑:
    - 任意一次 passed=False → 该能力 consecutive_failures += 1
    - 连续失败达阈值 (默认 2) → level 强制降回 1, auto_enabled = False (I2)
    - 任意一次 passed=True  → 清零连续失败;若原 level >= 2 视为保持(晋升由人工/SLA 决定)
    """
    if scenario not in _SCENARIOS:
        msg = f"unknown drill scenario: {scenario}"
        raise ValueError(msg)

    await conn.execute(
        """
        INSERT INTO drill_results (capability, scenario, passed, detail)
        VALUES ($1, $2, $3, $4)
        """,
        capability,
        scenario,
        passed,
        detail,
    )

    # 更新 maturity 行 (upsert)
    row = await conn.fetchrow(
        "SELECT level, consecutive_failures, auto_enabled FROM capability_maturity WHERE capability = $1",
        capability,
    )
    if row is None:
        level = 1
        consecutive = 0
        auto_enabled = False
    else:
        level = row["level"]
        consecutive = row["consecutive_failures"]
        auto_enabled = row["auto_enabled"]

    if passed:
        consecutive = 0
    else:
        consecutive += 1
        if consecutive >= threshold and level > 1:
            log.error(
                "maturity_downgraded capability=%s level=%d->1 reason=consecutive_failures=%d (§2.1)",
                capability,
                level,
                consecutive,
            )
            level = 1
            auto_enabled = False

    await conn.execute(
        """
        INSERT INTO capability_maturity
            (capability, level, consecutive_failures, auto_enabled, last_drill_at, updated_at)
        VALUES ($1, $2, $3, $4, now(), now())
        ON CONFLICT (capability) DO UPDATE
            SET level = EXCLUDED.level,
                consecutive_failures = EXCLUDED.consecutive_failures,
                auto_enabled = EXCLUDED.auto_enabled,
                last_drill_at = now(),
                updated_at = now()
        """,
        capability,
        level,
        consecutive,
        auto_enabled,
    )

    return {
        "capability": capability,
        "level": level,
        "consecutive_failures": consecutive,
        "auto_enabled": auto_enabled,
        "downgraded": (passed is False and level == 1 and consecutive >= threshold),
    }


async def capability_state(conn: Any, capability: str) -> dict[str, Any]:
    """查询某能力当前成熟度 + auto 许可状态。缺行 → 默认 L1, auto 禁用 (负债默认)。"""
    row = await conn.fetchrow(
        """
        SELECT level, consecutive_failures, auto_enabled, last_drill_at, reason
        FROM capability_maturity WHERE capability = $1
        """,
        capability,
    )
    if row is None:
        return {
            "capability": capability,
            "level": 1,
            "consecutive_failures": 0,
            "auto_enabled": False,
            "last_drill_at": None,
            "reason": "never drilled — 默认 L1 负债, auto 禁用 (§2.1/I2)",
        }
    return {
        "capability": capability,
        "level": row["level"],
        "consecutive_failures": row["consecutive_failures"],
        "auto_enabled": bool(row["auto_enabled"]),
        "last_drill_at": row["last_drill_at"],
        "reason": row["reason"],
    }


async def is_auto_allowed(conn: Any, capability: str, *, require_l2: bool = True) -> bool:
    """该能力是否允许 auto 模式 (§5.4 / I2)。

    - require_l2=True (默认): level >= 2 且未连续失败降级方可 auto。
    - 降级后 auto_enabled=False → 返回 False。
    """
    state = await capability_state(conn, capability)
    if not state["auto_enabled"]:
        return False
    return not (require_l2 and state["level"] < 2)
