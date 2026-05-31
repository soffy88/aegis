"""Tests for webhook_subscriptions router — C2-5. Unit tests (mocked DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SUB_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_DELIVERY_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _user(role: str = "member") -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )


def _sub_row(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        sub_id=_SUB_ID,
        org_id=_ORG,
        name="my-hook",
        url="https://example.com/webhook",
        secret_encrypted=None,
        event_types=["alert.fired"],
        retry_count=3,
        retry_backoff_seconds=[5, 15, 45],
        enabled=True,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(kwargs)
    return base


def _delivery_row(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        delivery_id=_DELIVERY_ID,
        sub_id=_SUB_ID,
        org_id=_ORG,
        event_type="alert.fired",
        payload={"rule_id": "abc"},
        attempt_no=0,
        max_attempts=4,
        next_attempt_at=_NOW,
        last_attempt_at=None,
        last_status_code=None,
        last_error=None,
        state="pending",
        created_at=_NOW,
        succeeded_at=None,
    )
    base.update(kwargs)
    return base


def _make_app() -> FastAPI:
    from aegis.server.api.routers import webhook_subscriptions

    fa = FastAPI()
    fa.include_router(webhook_subscriptions.router)
    return fa


def _add_db(fa: FastAPI, conn: mock.AsyncMock) -> None:
    async def _override() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _override


def _set_user(fa: FastAPI, role: str = "member") -> None:
    u = _user(role)

    async def _override() -> UserContext:
        return u

    fa.dependency_overrides[get_current_user] = _override


class TestWebhookRouterAuth:
    def test_create_requires_configure_notify_viewer_403(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={"name": "h", "url": "https://x.com", "event_types": ["alert.fired"]},
        )
        assert r.status_code == 403

    def test_list_requires_view_org_viewer_allowed(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(return_value=[])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/webhooks")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_member_allowed(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_sub_row())
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={
                "name": "my-hook",
                "url": "https://example.com/webhook",
                "event_types": ["alert.fired"],
            },
        )
        assert r.status_code == 201
        assert r.json()["name"] == "my-hook"

    def test_no_auth_returns_401(self) -> None:
        fa = _make_app()
        _add_db(fa, mock.AsyncMock())
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/webhooks")
        assert r.status_code == 401


class TestWebhookRouterValidation:
    def test_invalid_event_type_returns_422(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={"name": "h", "url": "https://x.com", "event_types": ["unknown.event"]},
        )
        assert r.status_code == 422

    def test_invalid_url_scheme_returns_422(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={"name": "h", "url": "ftp://example.com", "event_types": ["alert.fired"]},
        )
        assert r.status_code == 422

    def test_ssrf_private_ip_returns_422(self) -> None:
        """Literal private-range IP in url is rejected at validation time."""
        fa = _make_app()
        conn = mock.AsyncMock()
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={
                "name": "h",
                "url": "http://169.254.169.254/secret",
                "event_types": ["alert.fired"],
            },
        )
        assert r.status_code == 422

    def test_ssrf_loopback_ip_returns_422(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={"name": "h", "url": "http://127.0.0.1/admin", "event_types": ["alert.fired"]},
        )
        assert r.status_code == 422

    def test_has_secret_in_response_not_secret_encrypted(self) -> None:
        """Response contains has_secret bool, never the raw secret value."""
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_sub_row(secret_encrypted="plain:abc"))
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={
                "name": "my-hook",
                "url": "https://example.com/webhook",
                "event_types": ["alert.fired"],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert "secret_encrypted" not in body
        assert body["has_secret"] is True


class TestWebhookTestEndpoint:
    def test_test_endpoint_enqueues_delivery(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        # sub exists
        conn.fetchrow = mock.AsyncMock(return_value=_sub_row())
        # enqueue returns delivery row
        conn.fetchrow = mock.AsyncMock(
            side_effect=[_sub_row(), _delivery_row(event_type="webhook.test")]
        )
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.post(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}/test")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "enqueued"

    def test_test_endpoint_404_if_sub_missing(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}/test")
        assert r.status_code == 404


class TestWebhookDeliveriesEndpoint:
    def test_list_deliveries_viewer_allowed(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_sub_row())
        conn.fetch = mock.AsyncMock(return_value=[_delivery_row()])
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa)
        r = c.get(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}/deliveries")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["event_type"] == "alert.fired"

    def test_get_webhook_404(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}")
        assert r.status_code == 404

    def test_delete_returns_204(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value="DELETE 1")
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.delete(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}")
        assert r.status_code == 204

    def test_delete_404_if_not_found(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.execute = mock.AsyncMock(return_value="DELETE 0")
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.delete(f"/api/v1/orgs/{_ORG}/webhooks/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_update_returns_200(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=_sub_row(retry_count=5))
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa)
        r = c.patch(
            f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}",
            json={"retry_count": 5},
        )
        assert r.status_code == 200
        assert r.json()["retry_count"] == 5

    def test_update_404_if_not_found(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.patch(
            f"/api/v1/orgs/{_ORG}/webhooks/{uuid.uuid4()}",
            json={"retry_count": 5},
        )
        assert r.status_code == 404

    def test_create_409_on_duplicate_name(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(side_effect=Exception("unique constraint violated"))
        _add_db(fa, conn)
        _set_user(fa, "member")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.post(
            f"/api/v1/orgs/{_ORG}/webhooks",
            json={"name": "dup", "url": "https://x.com", "event_types": ["alert.fired"]},
        )
        assert r.status_code == 409

    def test_list_deliveries_404_if_sub_missing(self) -> None:
        fa = _make_app()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)
        _add_db(fa, conn)
        _set_user(fa, "viewer")
        c = TestClient(fa, raise_server_exceptions=False)
        r = c.get(f"/api/v1/orgs/{_ORG}/webhooks/{_SUB_ID}/deliveries")
        assert r.status_code == 404
