"""Tests for alert_rules + alert_fired routers — C2-2. Unit tests (mocked DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import alert_fired as alert_fired_router
from aegis.server.api.routers import alert_rules as alert_rules_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("77777777-7777-7777-7777-777777777777")
_PROJ = uuid.UUID("88888888-8888-8888-8888-888888888888")
_USER = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
_RULE_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _user(role: str) -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )


def _rule_row(**kwargs: object) -> dict:
    base = dict(
        rule_id=_RULE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        name="cpu-rule",
        metric="container.cpu.percent",
        threshold_warn=70.0,
        threshold_critical=90.0,
        operator=">=",
        throttle_seconds=300,
        escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600,
        enabled=True,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(kwargs)
    return base


def _fired_row(**kwargs: object) -> dict:
    base = dict(
        fired_id=uuid.uuid4(),
        rule_id=_RULE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        dedup_key="abc123",
        severity="warn",
        current_value=75.0,
        triggered_reason="cpu >= 70",
        fired_at=_NOW,
        escalated_at=None,
        last_seen_at=_NOW,
    )
    base.update(kwargs)
    return base


def _make_app(*routers: object) -> FastAPI:
    fa = FastAPI()
    for r in routers:
        fa.include_router(r)  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# alert-rules router
# ---------------------------------------------------------------------------


class TestAlertRulesRouterAuth:
    def test_list_no_auth_returns_401(self) -> None:
        fa = _make_app(alert_rules_router.router)
        _add_db(fa, mock.AsyncMock())
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules")
        assert r.status_code == 401

    def test_list_viewer_allowed(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(return_value=[])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules")
        assert r.status_code == 200

    def test_create_viewer_returns_403(self) -> None:
        fa = _make_app(alert_rules_router.router)
        _add_db(fa, mock.AsyncMock())
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules",
            json={"name": "x", "metric": "m", "threshold_critical": 90.0},
        )
        assert r.status_code == 403

    def test_create_member_allowed(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_rule_row())
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules",
            json={
                "name": "cpu-rule",
                "metric": "container.cpu.percent",
                "threshold_critical": 90.0,
            },
        )
        assert r.status_code == 201
        assert r.json()["name"] == "cpu-rule"

    def test_get_404_not_found(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_update_partial_member_allowed(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        updated = _rule_row(threshold_warn=80.0)
        conn.fetchrow = mock.AsyncMock(return_value=updated)
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.patch(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules/{_RULE_ID}",
            json={"threshold_warn": 80.0},
        )
        assert r.status_code == 200
        assert r.json()["threshold_warn"] == pytest.approx(80.0)

    def test_delete_member_allowed(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value="DELETE 1")
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.delete(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules/{_RULE_ID}")
        assert r.status_code == 204

    def test_delete_not_found_returns_404(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value="DELETE 0")
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.delete(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_create_duplicate_returns_409(self) -> None:
        import asyncpg

        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(
            side_effect=asyncpg.UniqueViolationError("duplicate key value")
        )
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules",
            json={"name": "dup", "metric": "m", "threshold_critical": 90.0},
        )
        assert r.status_code == 409

    def test_get_success_returns_rule(self) -> None:
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_rule_row())
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alert-rules/{_RULE_ID}")
        assert r.status_code == 200
        assert r.json()["name"] == "cpu-rule"

    def test_get_wrong_project_returns_404(self) -> None:
        other_proj = uuid.uuid4()
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        # Rule exists for _PROJ but request targets other_proj
        conn.fetchrow = mock.AsyncMock(return_value=_rule_row())
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{other_proj}/alert-rules/{_RULE_ID}")
        assert r.status_code == 404

    def test_update_wrong_project_returns_404(self) -> None:
        other_proj = uuid.uuid4()
        fa = _make_app(alert_rules_router.router)
        conn = mock.AsyncMock()
        # Update returns a rule belonging to _PROJ, but URL says other_proj
        conn.fetchrow = mock.AsyncMock(return_value=_rule_row())
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.patch(
            f"/api/v1/orgs/{_ORG}/projects/{other_proj}/alert-rules/{_RULE_ID}",
            json={"threshold_warn": 80.0},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# alert-fired router
# ---------------------------------------------------------------------------


class TestAlertFiredRouter:
    def test_list_no_auth_returns_401(self) -> None:
        fa = _make_app(alert_fired_router.router)
        _add_db(fa, mock.AsyncMock())
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alerts-fired")
        assert r.status_code == 401

    def test_list_viewer_allowed(self) -> None:
        fa = _make_app(alert_fired_router.router)
        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(return_value=[_fired_row()])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alerts-fired")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["severity"] == "warn"

    def test_list_severity_filter_passed(self) -> None:
        fa = _make_app(alert_fired_router.router)
        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(return_value=[_fired_row(severity="critical")])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}/alerts-fired?severity=critical")
        assert r.status_code == 200
        assert r.json()[0]["severity"] == "critical"
