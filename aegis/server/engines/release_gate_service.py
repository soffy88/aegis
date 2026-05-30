"""Release Gate Service — 服务层封装.

供 router + C2-3 AutoHeal Engine 调用.
不做: HTTP 路由 / DB SQL / AutoHeal 状态机集成 (C2-3 范围).
"""

from __future__ import annotations

import uuid
from typing import Any

from aegis.server.repositories.release_gate_repository import ReleaseGateRepository
from aegis.server.schemas.release_gate import ReleaseGateResponse


class ReleaseGateService:
    def __init__(self, repo: ReleaseGateRepository) -> None:
        self.repo = repo

    async def create_gate(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        requested_by: uuid.UUID,
        action_kind: str,
        action_payload: dict[str, Any],
        autoheal_event_id: uuid.UUID | None = None,
        expires_in_hours: int = 24,
    ) -> ReleaseGateResponse:
        """Create a release_gate (called by C2-3 AutoHeal Engine or manually)."""
        return await self.repo.create(
            org_id=org_id,
            project_id=project_id,
            requested_by=requested_by,
            action_kind=action_kind,
            action_payload=action_payload,
            autoheal_event_id=autoheal_event_id,
            expires_in_hours=expires_in_hours,
        )

    async def approve(
        self,
        *,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        decided_by: uuid.UUID,
        decision_reason: str,
    ) -> ReleaseGateResponse | None:
        """Approve a release_gate. Returns None if expired/already decided/not found."""
        return await self.repo.decide(
            gate_id=gate_id,
            org_id=org_id,
            decided_by=decided_by,
            decision="approved",
            decision_reason=decision_reason,
        )

    async def reject(
        self,
        *,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        decided_by: uuid.UUID,
        decision_reason: str,
    ) -> ReleaseGateResponse | None:
        """Reject a release_gate. Returns None if expired/already decided/not found."""
        return await self.repo.decide(
            gate_id=gate_id,
            org_id=org_id,
            decided_by=decided_by,
            decision="rejected",
            decision_reason=decision_reason,
        )

    async def get_active_gate_by_event(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        autoheal_event_id: uuid.UUID,
    ) -> ReleaseGateResponse | None:
        """Return the pending gate for a given autoheal_event_id, if any.

        Called by C2-3 AutoHeal Engine to check whether a gate blocks execution.
        lazy_expire in list_by_project will mark stale gates expired before returning.
        """
        gates = await self.repo.list_by_project(
            org_id=org_id,
            project_id=project_id,
            state="pending",
        )
        return next((g for g in gates if g.autoheal_event_id == autoheal_event_id), None)
