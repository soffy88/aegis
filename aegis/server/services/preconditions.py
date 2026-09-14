"""§11 / §2.1 运行前置条件评估与 degraded mode 判定 (DESIGN C-11, C-2.1).

系统满足 §11 五条前置条件之前 MUST NOT 视为 operational,且 MUST 以 degraded mode
运行(§11.1):全部自愈 auto 模式禁用、R1 及以上动作全部人工门、Brain 仅只读。

本模块把"前置条件满足度"做成一个可被编排层、脑、autoheal、各破坏性动作入口查询的
单一事实源。条件本身由部署侧核实(REVIEW 类),故通过 `aegis_platform_flags` 表达:
- `precond:exposure`     （I7 暴露收口：tunnel 前 Cloudflare Access 策略）
- `precond:deadman`      （I4 外部死人开关已接入并验证静默触发）
- `precond:dataguard`    （I6 保留/降采样/存储守卫 70% 已生效）
- `precond:self_recover` （自身 DB 已备份且完成一次真实 restore 演练）
- `precond:m2_isolation` （M2 Aegis PG 与被管 DB 非同实例）

任一未满足 → 进入 degraded mode。运维经 API/SQL 逐项置位;全部满足方可退出。
这些 flag 缺行 = 未满足(默认降级,而非默认放行——与 kill-switch 反义,因为降级是
"更保守"的默认态)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# 前置条件 flag 的 key 前缀。逐一置位 = 该项已核实满足。
_PRECOND_PREFIX = "precond:"

# §11 五项,顺序即文档陈述顺序。
PRECOND_EXPOSURE = f"{_PRECOND_PREFIX}exposure"
PRECOND_DEADMAN = f"{_PRECOND_PREFIX}deadman"
PRECOND_DATAGUARD = f"{_PRECOND_PREFIX}dataguard"
PRECOND_SELF_RECOVER = f"{_PRECOND_PREFIX}self_recover"
PRECOND_M2_ISOLATION = f"{_PRECOND_PREFIX}m2_isolation"

ALL_PRECONDITIONS = (
    PRECOND_EXPOSURE,
    PRECOND_DEADMAN,
    PRECOND_DATAGUARD,
    PRECOND_SELF_RECOVER,
    PRECOND_M2_ISOLATION,
)

_PRECOND_LABELS = {
    PRECOND_EXPOSURE: "I7 暴露收口 (tunnel 前 Cloudflare Access 策略)",
    PRECOND_DEADMAN: "I4 外部死人开关已接入并验证静默触发",
    PRECOND_DATAGUARD: "I6 保留/降采样/存储守卫 70% 已生效",
    PRECOND_SELF_RECOVER: "自身 DB 已备份且完成一次真实 restore 演练",
    PRECOND_M2_ISOLATION: "M2 Aegis PG 与被管 DB 非同实例",
}


@dataclass
class PreconditionState:
    """单条前置条件的满足情况。缺行 = 未满足 (默认降级)。"""

    key: str
    label: str
    satisfied: bool
    reason: str | None


@dataclass
class SystemReadiness:
    """系统运营就绪态汇总。"""

    degraded: bool
    preconditions: list[PreconditionState]

    @property
    def unsatisfied(self) -> list[PreconditionState]:
        return [p for p in self.preconditions if not p.satisfied]


async def get_precondition(conn: Any, key: str) -> PreconditionState:
    """读取单条前置条件。缺行 = 未满足(默认降级)。"""
    from aegis.server.services.platform_flags import get_flag  # noqa: PLC0415

    flag = await get_flag(conn, key)
    return PreconditionState(
        key=key,
        label=_PRECOND_LABELS.get(key, key),
        satisfied=bool(flag["enabled"]),
        reason=flag["reason"],
    )


async def get_readiness(conn: Any) -> SystemReadiness:
    """汇总全部前置条件并判定 degraded mode。

    §11.1: 任一未满足 → degraded = True。这是保守默认,因为降级态比"假装 operational"
    更安全。全部满足时 degraded = False。
    """
    states = [await get_precondition(conn, k) for k in ALL_PRECONDITIONS]
    degraded = any(not s.satisfied for s in states)
    return SystemReadiness(degraded=degraded, preconditions=states)


async def set_precondition(
    conn: Any, key: str, *, satisfied: bool, reason: str | None = None
) -> None:
    """运维经 API/SQL 置位/解除单条前置条件。"""
    if key not in ALL_PRECONDITIONS:
        msg = f"unknown precondition key: {key}"
        raise ValueError(msg)
    from aegis.server.services.platform_flags import set_flag  # noqa: PLC0415

    await set_flag(conn, key, enabled=satisfied, reason=reason)
    log.warning("precondition_changed key=%s satisfied=%s reason=%s", key, satisfied, reason)


async def assert_not_degraded(conn: Any, *, action: str) -> None:
    """破坏性/自动动作入口的统一降级闸门 (C-11)。

    在 degraded mode 下,任何 auto 自愈、R1+ 破坏性动作、未过 L2 的 Execute 能力，
    MUST 被此断言拒绝(fail-closed)。调用方传入动作语义标签便于错误提示与审计。
    """
    readiness = await get_readiness(conn)
    if readiness.degraded:
        from aegis.server.exceptions import DegradedModeError  # noqa: PLC0415

        detail = ", ".join(s.label for s in readiness.unsatisfied)
        raise DegradedModeError(
            f"degraded_mode: action '{action}' blocked — 前置条件未满足: {detail}。"
            "修复 §11 前置条件并逐项置位后方可执行。"
        )
