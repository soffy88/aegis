"""Brain / RCA debugging API."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.brain.action_planner import get_planner_service, propose_action_plan
from aegis.server.brain.rca import get_rca_service, investigate_if_deep_needed
from aegis.server.brain.triage import get_triage_service, triage_signal
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/brain", tags=["brain"])


class TriageRequest(BaseModel):
    signal: dict[str, Any]


class InvestigateRequest(BaseModel):
    diagnose_result: dict[str, Any]


class PlanRequest(BaseModel):
    investigation_result: dict[str, Any]


@router.post("/triage")
async def manual_triage(
    org_id: uuid.UUID,
    req: TriageRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Manually trigger triage for a signal."""
    try:
        return await triage_signal(req.signal)
    except Exception as exc:
        log.exception("manual_triage_failed")
        return {"error": str(exc), "fallback": True}


@router.post("/investigate")
async def manual_investigate(
    org_id: uuid.UUID,
    req: InvestigateRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Manually trigger agentic RCA investigation."""
    cfg = get_settings()
    try:
        result = await investigate_if_deep_needed(req.diagnose_result, cfg, org_id=str(org_id))
        if result is None:
            return {
                "status": "skipped",
                "reason": "Gate function returned None (budget/severity/not-needed)",
            }
        return result
    except Exception as exc:
        log.exception("manual_investigate_failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/plan")
async def manual_plan(
    org_id: uuid.UUID,
    req: PlanRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> Any:
    """Manually trigger action planning."""
    try:
        # propose_action_plan expects symptom (str) and context (dict)
        # Investigation result usually has final_answer or similar
        res = req.investigation_result
        symptom = res.get("final_answer") or res.get("symptom") or "Unknown symptom"
        return await propose_action_plan(symptom, context=res)
    except Exception as exc:
        log.exception("manual_plan_failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/status")
async def brain_status(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Get health status of brain agents."""
    triage = get_triage_service()
    rca = get_rca_service()
    planner = get_planner_service()

    return {
        "triage": triage.health() if triage else {"status": "not_initialized"},
        "rca": rca.health() if rca else {"status": "not_initialized"},
        "planner": planner.health() if planner else {"status": "not_initialized"},
    }


@router.get("/spend")
async def get_llm_spend(
    org_id: uuid.UUID,
    days: float = Query(default=30, gt=0, le=365),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict:
    """Per-org LLM spend (total + per-model) over the last `days`. viewer+."""
    from aegis.server.services.llm_cost import org_spend  # noqa: PLC0415

    return await org_spend(conn, org_id=org_id, days=days)
