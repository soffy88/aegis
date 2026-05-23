"""Tests for App Store endpoints (BATCH 19 §B)."""

from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from aegis.server.app import create_app

client = TestClient(create_app())

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


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_returns_paginated(mock_load: mock.MagicMock) -> None:
    resp = client.get("/api/v1/store/apps?per_page=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["page"] == 1


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_search(mock_load: mock.MagicMock) -> None:
    resp = client.get("/api/v1/store/apps?q=redis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["slug"] == "redis"


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_list_apps_category_filter(mock_load: mock.MagicMock) -> None:
    resp = client.get("/api/v1/store/apps?category=Database")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_get_app_by_slug(mock_load: mock.MagicMock) -> None:
    resp = client.get("/api/v1/store/apps/nginx")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Nginx"


@mock.patch("aegis.server.api.routers.store._load_apps", return_value=MOCK_APPS)
def test_get_app_not_found(mock_load: mock.MagicMock) -> None:
    resp = client.get("/api/v1/store/apps/nonexistent")
    assert resp.status_code == 404
