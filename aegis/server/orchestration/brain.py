"""Brain orchestrator: chain Triage → RCA → Action Plan via dispatcher.

Step 15 §2.4:
- Three omodul chained at service layer (not inside a single omodul)
- Service layer decides escalation between stages
- omodul never calls omodul (H1)
- Service layer never replaces omodul 4 pillars
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.server.dispatch.omodul_dispatcher import OmodulDispatcher

log = logging.getLogger(__name__)


async def run_brain_pipeline(
    *,
    dispatcher: OmodulDispatcher,
    alert_payload: dict[str, Any],
    context: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    """Run Triage → RCA → Action Plan chain.

    Returns dict with stage markers + sub-results.
    """
    log.info("brain_pipeline_start user=%s alert=%s", user_id, alert_payload.get("alert_name"))

    # === Stage 1: Triage ===
    triage_result = await dispatcher.invoke(
        omodul_name="triage_signal",
        config={
            "signal_hash": _hash(alert_payload),
            "context_hash": _hash(context),
        },
        input_data={
            "signal": alert_payload,
            "context": context,
        },
        user_id=user_id,
    )

    if triage_result.get("status") != "completed":
        return {"stage": "triage_failed", "triage": triage_result}

    findings = triage_result.get("findings")
    should_escalate = getattr(findings, "should_escalate", False) if findings else False

    if not should_escalate:
        return {"stage": "triage_only", "triage": triage_result}

    # === Stage 2: RCA ===
    rca_result = await dispatcher.invoke(
        omodul_name="diagnose_root_cause",
        config={
            "signal_hash": _hash(alert_payload),
            "available_tools_hash": _hash(context.get("available_tools", [])),
            "max_steps": 20,
        },
        input_data={
            "signal": alert_payload,
            "available_tool_names": context.get("available_tools", []),
            "initial_context": context,
        },
        user_id=user_id,
    )

    if rca_result.get("status") != "completed":
        return {"stage": "rca_failed", "triage": triage_result, "rca": rca_result}

    rca_findings = rca_result.get("findings")
    if rca_findings and getattr(rca_findings, "requires_human", False):
        return {"stage": "rca_requires_human", "triage": triage_result, "rca": rca_result}

    # === Stage 3: Action Plan ===
    action_result = await dispatcher.invoke(
        omodul_name="propose_action_plan",
        config={
            "root_cause_hash": _hash(rca_findings) if rca_findings else "",
            "plugin_marketplace_version": context.get("plugin_marketplace_version", "v1"),
        },
        input_data={
            "root_cause": rca_findings.model_dump() if hasattr(rca_findings, "model_dump") else {},
            "available_plugins": context.get("available_plugins", []),
            "historical_resolutions": context.get("historical_resolutions", []),
        },
        user_id=user_id,
    )

    return {
        "stage": "action_plan_ready",
        "triage": triage_result,
        "rca": rca_result,
        "action_plan": action_result,
    }


def _hash(data: Any) -> str:
    """Hash using oprim utilities."""
    from oprim import canonical_json, sha256_hash

    if isinstance(data, (dict, list)):
        return sha256_hash(canonical_json(data))
    return sha256_hash(str(data))
