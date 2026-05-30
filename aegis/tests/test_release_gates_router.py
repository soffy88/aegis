"""Tests for release_gates router — C2-4a. Unit tests (mocked DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import release_gates as release_gates_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("cccc0001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("cccc0002-0000-0000-0000-000000000000")
_USER = uuid.UUID("cccc0003-0000-0000-0000-000000000000")
_GATE_ID = uuid.UUID("cccc0004-0000-0000-0000-000000000000")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _user(role: str) -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )


def _gate_row(**kwargs: object) -> dict:
    base = dict(
        gate_id=_GATE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        autoheal_event_id=None,
        action_kind="restart_container",
        action_payload={"container": "nginx"},
        requested_by=_USER,
        requested_at=_NOW,
        state="pending",
        decided_by=None,
        decided_at=None,
        decision_reason=None,
        expires_at=_NOW + timedelta(hours=24),
    )
    base.update(kwargs)
    return base


def _make_app() -> FastAPI:
    fa = FastAPI()
    fa.include_router(release_gates_router.router)
    return fa


def _add_db(fa: FastAPI, conn: mock.AsyncMock) -> None:
    async def _override() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _override


def _set_user(fa: FastAPI, role: str) -> None:
    u = _user(role)

    async def _override() -> UserContext:
        return u

    fa.dependency_overrides[get_current_user] = _override


class TestCreateReleaseGate:
    def test_no_auth_returns_401(self) -> None:
        fa = _make_app()
        _add_db(fa, mock.AsyncMock())
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates",
            json={"action_kind": "restart_container"},
        )
        assert r.status_code == 401

    def test_viewer_returns_403(self) -> None:
        fa = _make_app()
        _add_db(fa, mock.AsyncMock())
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates",
            json={"action_kind": "restart_container"},
        )
        assert r.status_code == 403

    def test_operator_creates_201(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_gate_row())
        _add_db(fa, conn)
        _set_user(fa, "operator")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates",
            json={"action_kind": "restart_container"},
        )
        assert r.status_code == 201
        assert r.json()["state"] == "pending"
        assert r.json()["action_kind"] == "restart_container"

    def test_empty_action_kind_returns_422(self) -> None:
        fa = _make_app()
        _add_db(fa, mock.AsyncMock())
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates",
            json={"action_kind": ""},
        )
        assert r.status_code == 422

    def test_duplicate_autoheal_event_returns_409(self) -> None:
        import asyncpg

        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(
            side_effect=asyncpg.UniqueViolationError("uq_release_gates_autoheal_event_id")
        )
        _add_db(fa, conn)
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates",
            json={"action_kind": "restart_container", "autoheal_event_id": str(uuid.uuid4())},
        )
        assert r.status_code == 409


class TestListReleaseGates:
    def test_viewer_can_list(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetch = mock.AsyncMock(return_value=[_gate_row()])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["state"] == "pending"

    def test_state_filter_passed(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetch = mock.AsyncMock(return_value=[_gate_row(state="approved")])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates?state=approved")
        assert r.status_code == 200
        assert r.json()[0]["state"] == "approved"


class TestGetReleaseGate:
    def test_get_success(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetchrow = mock.AsyncMock(return_value=_gate_row())
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}")
        assert r.status_code == 200
        assert r.json()["gate_id"] == str(_GATE_ID)

    def test_get_404_not_found(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_get_wrong_project_404(self) -> None:
        other_proj = uuid.uuid4()
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        # Gate belongs to _PROJ but request uses other_proj
        conn.fetchrow = mock.AsyncMock(return_value=_gate_row())
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{other_proj}/release-gates/{_GATE_ID}")
        assert r.status_code == 404


class TestDecideReleaseGate:
    def _conn_for_decide(
        self,
        existing_row: dict | None,
        decide_row: dict | None,
        latest_row: dict | None = None,
    ) -> mock.AsyncMock:
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        # fetchrow called multiple times: get (check project), decide, get (409 detail)
        conn.fetchrow = mock.AsyncMock(
            side_effect=[existing_row, decide_row]
            + ([latest_row] if latest_row is not None else [])
        )
        return conn

    def test_approve_success(self) -> None:
        approved = _gate_row(
            state="approved", decided_by=_USER, decided_at=_NOW, decision_reason="ok"
        )
        fa = _make_app()
        _add_db(fa, self._conn_for_decide(existing_row=_gate_row(), decide_row=approved))
        _set_user(fa, "operator")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}/decide",
            json={"decision": "approved", "decision_reason": "ok"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "approved"

    def test_reject_success(self) -> None:
        rejected = _gate_row(
            state="rejected", decided_by=_USER, decided_at=_NOW, decision_reason="no"
        )
        fa = _make_app()
        _add_db(fa, self._conn_for_decide(existing_row=_gate_row(), decide_row=rejected))
        _set_user(fa, "operator")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}/decide",
            json={"decision": "rejected", "decision_reason": "no"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "rejected"

    def test_decide_not_found_returns_404(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{uuid.uuid4()}/decide",
            json={"decision": "approved", "decision_reason": "ok"},
        )
        assert r.status_code == 404

    def test_decide_expired_returns_409(self) -> None:
        expired_row = _gate_row(state="expired")
        fa = _make_app()
        # First fetchrow = existing check (project match), second = decide returns None,
        # third = latest for 409 detail
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetchrow = mock.AsyncMock(side_effect=[_gate_row(), None, expired_row])
        _add_db(fa, conn)
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}/decide",
            json={"decision": "approved", "decision_reason": "ok"},
        )
        assert r.status_code == 409

    def test_decide_already_decided_returns_409(self) -> None:
        already = _gate_row(
            state="approved", decided_by=_USER, decided_at=_NOW, decision_reason="done"
        )
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value=None)
        conn.fetchrow = mock.AsyncMock(side_effect=[_gate_row(), None, already])
        _add_db(fa, conn)
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}/decide",
            json={"decision": "rejected", "decision_reason": "changed mind"},
        )
        assert r.status_code == 409

    def test_decide_empty_reason_returns_422(self) -> None:
        fa = _make_app()
        _add_db(fa, mock.AsyncMock())
        _set_user(fa, "operator")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/release-gates/{_GATE_ID}/decide",
            json={"decision": "approved", "decision_reason": ""},
        )
        assert r.status_code == 422
