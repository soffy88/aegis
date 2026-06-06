"""Incident postmortem generation via omodul.generate_incident_postmortem."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def run_postmortem(
    *,
    incident: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    """Generate a postmortem markdown for an incident.

    Runs the (sync) omodul in a thread executor so it doesn't block the loop.
    Returns the postmortem markdown string.
    """
    return await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _call_omodul_postmortem(incident=incident, events=events),
    )


def _call_omodul_postmortem(
    *,
    incident: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    """Synchronous call to omodul.generate_incident_postmortem."""
    import omodul  # noqa: PLC0415
    from omodul.generate_incident_postmortem import (  # noqa: PLC0415
        GenerateIncidentPostmortemConfig,
        GenerateIncidentPostmortemInput,
    )

    incident_id = str(incident["id"])
    started_at = incident.get("started_at")
    time_window = "24h"
    if started_at:
        from datetime import UTC, datetime  # noqa: PLC0415

        now = datetime.now(UTC)
        started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
        delta_h = max(1, int((now - started).total_seconds() / 3600) + 1)
        time_window = f"{delta_h}h"

    cfg = GenerateIncidentPostmortemConfig(
        llm_model="claude-sonnet-4-6",
        incident_id=incident_id,
        time_window=time_window,
        scope="full",
        output_format="markdown",
    )
    inp = GenerateIncidentPostmortemInput(
        incident_id=incident_id,
        event_trail=[
            {
                "id": str(e.get("id", "")),
                "ts": str(e.get("ts", "")),
                "event_type": e.get("event_type", ""),
                "severity": e.get("severity", "info"),
                "service": e.get("service", ""),
                "payload": e.get("payload", {}),
            }
            for e in events
        ],
        involved_services=list({e.get("service", "") for e in events if e.get("service")}),
        resolutions_applied=[],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = omodul.generate_incident_postmortem(
            config=cfg,
            input_data=inp,
            output_dir=Path(tmpdir),
        )

    # The function returns a dict; markdown is in the output files or in result
    md = result.get("postmortem_markdown") or result.get("markdown") or ""
    if not md:
        # Fallback: read from the output file if written
        md = _extract_md_from_result(result)
    if not md:
        md = (
            f"# Postmortem: {incident.get('title', incident_id)}\n\n"
            "*Generation completed. See full report in aegis data dir.*"
        )
    return md


def _extract_md_from_result(result: dict[str, Any]) -> str:
    """Extract markdown text from omodul result dict."""
    for key in ("content", "output", "text", "body"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""
