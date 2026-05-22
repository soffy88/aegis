"""AutoHeal Engine dispatcher — SKELETON.

Wires plugin lookup + dry-run logging. Actual lifecycle execution
(pre_check/execute/post_verify/rollback) is added in a future batch
(needs concrete plugins + AutoHealContext implementation).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

from aegis.server.persistence import append_event

log = logging.getLogger(__name__)


class AutoHealDispatcher:
    """Match alerts to plugins and (optionally) dispatch them.

    Skeleton: loads plugins at init time, matches by name, logs the would-be
    invocation, and writes event_trail markers. Actual execution is gated on
    settings.autoheal_dry_run.
    """

    def __init__(self, plugins: dict[str, type[Any]], dry_run: bool = True) -> None:
        self._plugins = plugins
        self._dry_run = dry_run
        log.info("autoheal_dispatcher_ready plugins=%d dry_run=%s", len(plugins), dry_run)

    def find_matching_plugins(self, alert_name: str) -> list[type[Any]]:
        """Find plugins whose matches_alert pattern matches alert_name.

        v0.1: simple substring match. Glob support comes in a later batch.
        """
        matches = []
        for cls in self._plugins.values():
            pattern = getattr(cls, "matches_alert", "")
            if pattern and pattern in alert_name:
                matches.append(cls)
        return matches

    async def dispatch(
        self,
        *,
        conn: asyncpg.Connection,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        alert_payload: dict[str, Any],
        trace_id: str,
        parent_event_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Dispatch matching plugins for the alert.

        In dry-run mode, writes a `autoheal_triggered` event but does not
        invoke the plugin lifecycle.
        """
        alert_name = alert_payload.get("alert_name", "")
        matched = self.find_matching_plugins(alert_name)

        if not matched:
            return {"matched": [], "outcome": "no_match"}

        results: list[dict[str, Any]] = []
        for cls in matched:
            event_id = await append_event(
                conn=conn,
                org_id=org_id,
                project_id=project_id,
                event_type="autoheal_triggered",
                severity="warning",
                autoheal_plugin=cls.name,
                payload={
                    "matches_alert": cls.matches_alert,
                    "dry_run": self._dry_run,
                    "alert_payload": alert_payload,
                },
                trace_id=trace_id,
                parent_id=parent_event_id,
                initiated_by="agent",
            )

            if self._dry_run:
                log.info("autoheal_dry_run plugin=%s alert=%s", cls.name, alert_name)
                results.append(
                    {
                        "plugin": cls.name,
                        "outcome": "dry_run",
                        "event_id": str(event_id),
                    }
                )
                continue

            # TODO (future batch): build AutoHealContext + run lifecycle
            log.warning("autoheal_execute_not_implemented plugin=%s", cls.name)
            results.append({"plugin": cls.name, "outcome": "execute_stub"})

        return {"matched": [c.name for c in matched], "results": results}
