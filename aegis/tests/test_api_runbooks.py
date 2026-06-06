"""Tests for runbooks API — YAML + entry_points plugin merge."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


_YAML_RUNBOOK = {
    "name": "restart-service",
    "description": "Restart a failing service",
    "trigger": "service_down",
    "requires_approval": True,
    "steps": [],
    "source": "yaml",
}

_PLUGIN_ENTRY = {
    "name": "restart-container",
    "description": "Restart a specific Docker container",
    "trigger": "container_unhealthy",
    "requires_approval": False,
    "version": "1.0.0",
    "steps": [],
    "source": "plugin",
}


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(runbooks_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        yield m

    fa.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(fa) as c:
        yield c


class TestRunbooksListMerge:
    def test_list_returns_yaml_runbooks(self, client: TestClient) -> None:
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[mock.MagicMock(model_dump=lambda: {**_YAML_RUNBOOK})],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        assert r.status_code == 200
        items = r.json()
        assert any(rb["name"] == "restart-service" for rb in items)

    def test_list_includes_plugin_entries(self, client: TestClient) -> None:
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[_PLUGIN_ENTRY],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        assert r.status_code == 200
        items = r.json()
        assert any(rb["name"] == "restart-container" for rb in items)

    def test_list_merges_yaml_and_plugins(self, client: TestClient) -> None:
        yaml_rb = mock.MagicMock()
        yaml_rb.model_dump.return_value = {**_YAML_RUNBOOK}
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[yaml_rb],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[_PLUGIN_ENTRY],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 2

    def test_yaml_runbooks_have_source_yaml(self, client: TestClient) -> None:
        yaml_rb = mock.MagicMock()
        yaml_rb.model_dump.return_value = {**_YAML_RUNBOOK}
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[yaml_rb],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        items = r.json()
        assert all(rb["source"] == "yaml" for rb in items)

    def test_plugin_entries_have_source_plugin(self, client: TestClient) -> None:
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[_PLUGIN_ENTRY],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        items = r.json()
        assert all(rb["source"] == "plugin" for rb in items)

    def test_plugin_entries_have_trigger_from_matches_alert(self, client: TestClient) -> None:
        with (
            mock.patch(
                "aegis.server.api.routers.runbooks.list_runbooks",
                return_value=[],
            ),
            mock.patch(
                "aegis.server.api.routers.runbooks.list_plugins",
                return_value=[_PLUGIN_ENTRY],
            ),
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
        items = r.json()
        plugin_entry = next(rb for rb in items if rb["source"] == "plugin")
        assert plugin_entry["trigger"] == "container_unhealthy"
