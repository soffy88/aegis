"""Triage — stub pending oservice v0.4.2 triage engine implementation.

oservice v0.4.1 ships only EngineSkeleton for triage (injection_points = {}).
Real triage engine with on_signal trigger + LLM classification is targeted for
oservice v0.4.2.

S1.5 sprint: replace this stub with real TriageEngine assembly once v0.4.2 ships.

Stub contract: always returns should_escalate=True so the RCA/Planner chain
downstream can be tested end-to-end without a real triage decision.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def triage_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Stub triage: transparent pass-through until oservice v0.4.2 ships real engine.

    Returns should_escalate=True unconditionally so Brain chain can be exercised in S1.
    S1.5: replace with TriageEngine._classify_signal call.
    """
    log.warning(
        "triage_signal_stub signal_id=%s oservice_v0.4.1_triage_unimplemented "
        "transparent_passthrough (S1.5 真装配)",
        signal.get("signal_id"),
    )
    return {
        "should_escalate": True,
        "severity": signal.get("severity", "medium"),
        "reason": "stub (oservice v0.4.2 待 ship)",
        "classified_category": None,
        "cost_usd": 0.0,
    }
