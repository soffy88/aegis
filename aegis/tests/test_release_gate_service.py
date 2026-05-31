"""Tests for ReleaseGateService — C2-4a. Pure unit tests (mocked repo)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from aegis.server.engines.release_gate_service import ReleaseGateService
from aegis.server.schemas.release_gate import ReleaseGateResponse

_ORG = uuid.UUID("bbbb0001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("bbbb0002-0000-0000-0000-000000000000")
_USER = uuid.UUID("bbbb0003-0000-0000-0000-000000000000")
_GATE_ID = uuid.UUID("bbbb0004-0000-0000-0000-000000000000")
_EVENT_ID = uuid.UUID("bbbb0005-0000-0000-0000-000000000000")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _gate_resp(
    state: str = "pending",
    autoheal_event_id: uuid.UUID | None = None,
) -> ReleaseGateResponse:
    return ReleaseGateResponse.model_validate(
        dict(
            gate_id=_GATE_ID,
            org_id=_ORG,
            project_id=_PROJ,
            autoheal_event_id=autoheal_event_id,
            action_kind="restart_container",
            action_payload={"container": "nginx"},
            requested_by=_USER,
            requested_at=_NOW,
            state=state,
            decided_by=_USER if state in ("approved", "rejected") else None,
            decided_at=_NOW if state in ("approved", "rejected") else None,
            decision_reason="ok" if state in ("approved", "rejected") else None,
            expires_at=_NOW + timedelta(hours=24),
        )
    )


def _make_service(
    create_return: ReleaseGateResponse | None = None,
    decide_return: ReleaseGateResponse | None = None,
    list_return: list[ReleaseGateResponse] | None = None,
) -> ReleaseGateService:
    repo = MagicMock()
    repo.create = AsyncMock(return_value=create_return or _gate_resp())
    repo.decide = AsyncMock(return_value=decide_return)
    repo.list_by_project = AsyncMock(return_value=list_return or [])
    return ReleaseGateService(repo)


class TestReleaseGateService:
    async def test_create_gate(self) -> None:
        service = _make_service(create_return=_gate_resp())
        result = await service.create_gate(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={"container": "nginx"},
        )
        assert result.state == "pending"
        assert result.action_kind == "restart_container"

    async def test_approve_success(self) -> None:
        approved = _gate_resp("approved")
        service = _make_service(decide_return=approved)
        result = await service.approve(
            gate_id=_GATE_ID,
            org_id=_ORG,
            decided_by=_USER,
            decision_reason="approved by ops",
        )
        assert result is not None
        assert result.state == "approved"

    async def test_reject_success(self) -> None:
        rejected = _gate_resp("rejected")
        service = _make_service(decide_return=rejected)
        result = await service.reject(
            gate_id=_GATE_ID,
            org_id=_ORG,
            decided_by=_USER,
            decision_reason="too risky",
        )
        assert result is not None
        assert result.state == "rejected"

    async def test_get_active_gate_by_event_found(self) -> None:
        pending = _gate_resp("pending", autoheal_event_id=_EVENT_ID)
        service = _make_service(list_return=[pending])
        result = await service.get_active_gate_by_event(
            org_id=_ORG, project_id=_PROJ, autoheal_event_id=_EVENT_ID
        )
        assert result is not None
        assert result.autoheal_event_id == _EVENT_ID

    async def test_get_active_gate_by_event_none(self) -> None:
        service = _make_service(list_return=[])
        result = await service.get_active_gate_by_event(
            org_id=_ORG, project_id=_PROJ, autoheal_event_id=uuid.uuid4()
        )
        assert result is None

    async def test_approve_returns_none_when_expired(self) -> None:
        service = _make_service(decide_return=None)
        result = await service.approve(
            gate_id=_GATE_ID,
            org_id=_ORG,
            decided_by=_USER,
            decision_reason="after expiry",
        )
        assert result is None
