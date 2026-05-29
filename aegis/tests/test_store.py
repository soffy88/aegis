"""Tests for App Store endpoints (C1-4 org-scoped paths)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.routers import store as store_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

MOCK_APPS = [
    {
        "slug": "redis",
        "name": "Redis",
        "description": "In-memory store",
        "category": "Database",
        "icon": "🗄️",
    },
    {
        "slug": "nginx",
        "name": "Nginx",
        "description": "Web server",
        "category": "Web",
        "icon": "🌐",
    },
    {
        "slug": "postgres",
        "name": "PostgreSQL",
        "description": "Relational database",
        "category": "Database",
        "icon": "🐘",
    },
]


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(store_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with TestClient(fa) as c:
        yield c


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_returns_paginated(mock_load: mock.MagicMock, client: TestClient) -> None:
    resp = client.get(f"/api/v1/orgs/{_ORG}/store?per_page=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["page"] == 1


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_search(mock_load: mock.MagicMock, client: TestClient) -> None:
    resp = client.get(f"/api/v1/orgs/{_ORG}/store?q=redis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["slug"] == "redis"


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_category_filter(mock_load: mock.MagicMock, client: TestClient) -> None:
    resp = client.get(f"/api/v1/orgs/{_ORG}/store?category=Database")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_get_app_by_slug(mock_load: mock.MagicMock, client: TestClient) -> None:
    resp = client.get(f"/api/v1/orgs/{_ORG}/store/nginx")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Nginx"


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_get_app_not_found(mock_load: mock.MagicMock, client: TestClient) -> None:
    resp = client.get(f"/api/v1/orgs/{_ORG}/store/nonexistent")
    assert resp.status_code == 404
