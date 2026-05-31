"""Demo plugin: simulates a high-risk destructive action that always requires approval.

Used for C2-3 testing and approval workflow demonstration only.
Not wired to production autoheal triggers in M1.
"""

from __future__ import annotations

from typing import Any

from aegis.server.orchestration.autoheal import PluginResult


class DestructiveActionPlugin:
    """Demo plugin that requires human approval before executing."""

    name: str = "destructive_action_demo"
    action_kind: str = "destructive_action_demo"

    async def pre_check(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True, requires_restart_verify=False)

    async def requires_approval(self, context: dict[str, Any]) -> bool:
        return True  # always requires approval

    async def execute(self, context: dict[str, Any]) -> PluginResult:
        # no-op demo: no real destructive action performed
        return PluginResult(success=True, requires_restart_verify=False)

    async def rollback(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)
