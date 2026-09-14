"""System operations — emergency stop / resume (§5.3, §11)."""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, require_global_admin
from aegis.server.services.platform_flags import (
    AUTOHEAL_KILL_SWITCH,
    is_flag_enabled,
    set_flag,
)
from aegis.server.services.preconditions import (
    ALL_PRECONDITIONS,
    get_readiness,
    set_precondition,
)

router = APIRouter(tags=["system"])


@router.get("/api/v1/system/readiness")
async def get_readiness_endpoint(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_global_admin),
) -> dict[str, Any]:
    """§11.1 degraded-mode 状态汇总。

    返回当前是否处于降级模式 + 五条前置条件的满足度,供控制台/运维判断为何
    auto 自愈与破坏性动作被闸门拒绝。全局管理员可读。
    """
    readiness = await get_readiness(conn)
    return {
        "degraded": readiness.degraded,
        "preconditions": [
            {
                "key": p.key,
                "label": p.label,
                "satisfied": p.satisfied,
                "reason": p.reason,
            }
            for p in readiness.preconditions
        ],
    }


@router.put("/api/v1/system/preconditions/{key}")
async def put_precondition(
    key: str,
    body: dict[str, Any],
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_global_admin),
) -> dict[str, Any]:
    """运维置位/解除单条前置条件(platform-level)。

    逐项置位后,`/system/readiness` 的 degraded 会退出;全部满足方可退出 degraded mode。
    body: {"satisfied": true|false, "reason": "核实人/依据"}。
    """
    if key not in ALL_PRECONDITIONS:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"unknown precondition: {key}")
    satisfied = bool(body.get("satisfied", False))
    reason = body.get("reason")
    await set_precondition(conn, key, satisfied=satisfied, reason=reason)
    readiness = await get_readiness(conn)
    return {
        "key": key,
        "satisfied": satisfied,
        "degraded": readiness.degraded,
    }


@router.post("/api/v1/system/emergency-stop")
async def emergency_stop(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_global_admin),
) -> dict[str, Any]:
    """Global emergency stop: halt all autoheal immediately.

    Sets the AUTOHEAL_KILL_SWITCH flag to enabled. Any orchestration loop
    that reads this flag will skip all remediation actions until
    /api/v1/system/emergency-resume is called.
    """
    await set_flag(conn, AUTOHEAL_KILL_SWITCH, enabled=True, reason="emergency-stop via API")
    return {"status": "emergency_stop", "detail": "All autoheal halted via emergency-stop flag"}


@router.post("/api/v1/system/emergency-resume")
async def emergency_resume(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_global_admin),
) -> dict[str, Any]:
    """Clear the global emergency stop and resume autoheal."""
    if not await is_flag_enabled(conn, AUTOHEAL_KILL_SWITCH):
        return {"status": "no_emergency_stop", "detail": "Kill switch already clear"}
    await set_flag(conn, AUTOHEAL_KILL_SWITCH, enabled=False, reason="emergency-resume via API")
    return {"status": "emergency_resumed", "detail": "Autoheal resumed"}
