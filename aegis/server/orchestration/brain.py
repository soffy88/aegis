"""Brain three-agent pipeline (v0.6 §8) — SKELETON.

This batch only wires the dispatch flow. Actual LLM calls land in a future batch
(omodul.triage_alert / omodul.diagnose_root_cause / omodul.propose_runbook
need to be added to 3O main repo first).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

from aegis.server.persistence import append_event

log = logging.getLogger(__name__)


async def run_brain_pipeline(
    *,
    conn: asyncpg.Connection,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None,
    alert_payload: dict[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    """Brain three-stage pipeline: Triage → RCA → Runbook Proposer.

    Skeleton: writes event_trail markers for each stage. Actual LLM calls
    are stubbed (return placeholder findings).

    Returns:
        dict with stages_run + outcome.
    """
    log.info("brain_pipeline_start trace=%s alert=%s", trace_id,
             alert_payload.get("alert_name"))

    # === Stage 1: Triage (stub) ===
    triage_event_id = await append_event(
        conn=conn, org_id=org_id, project_id=project_id, user_id=user_id,
        event_type="omodul_run", severity="info",
        omodul_kind="triage_alert",
        payload={
            "stub": True,
            "would_call": "omodul.triage_alert",
            "alert_payload": alert_payload,
        },
        trace_id=trace_id, initiated_by="agent",
    )

    # Stub decision: escalate to RCA if severity >= warning
    triage_severity = alert_payload.get("severity", "info")
    should_escalate = triage_severity in ("warning", "critical")

    if not should_escalate:
        return {
            "stages_run": ["triage"],
            "outcome": "no_escalation_needed",
            "triage_event_id": str(triage_event_id),
        }

    # === Stage 2: RCA (stub) ===
    rca_event_id = await append_event(
        conn=conn, org_id=org_id, project_id=project_id, user_id=user_id,
        event_type="omodul_run", severity="info",
        omodul_kind="diagnose_root_cause",
        payload={
            "stub": True,
            "would_call": "omodul.diagnose_root_cause",
            "confidence_placeholder": 0.5,
        },
        trace_id=trace_id, parent_id=triage_event_id, initiated_by="agent",
    )

    # === Stage 3: Runbook Proposer (stub) ===
    runbook_event_id = await append_event(
        conn=conn, org_id=org_id, project_id=project_id, user_id=user_id,
        event_type="omodul_run", severity="info",
        omodul_kind="propose_runbook",
        payload={"stub": True, "would_call": "omodul.propose_runbook"},
        trace_id=trace_id, parent_id=rca_event_id, root_cause_id=rca_event_id,
        initiated_by="agent",
    )

    return {
        "stages_run": ["triage", "rca", "runbook"],
        "outcome": "pipeline_complete_stub",
        "triage_event_id": str(triage_event_id),
        "rca_event_id": str(rca_event_id),
        "runbook_event_id": str(runbook_event_id),
        "trace_id": trace_id,
    }
