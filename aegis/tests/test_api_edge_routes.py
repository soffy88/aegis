"""Tests for edge routes router — org-scoped Caddy route management."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.routers import edge as edge_router
from aegis.server.api.routers.edge import _org_prefix, _org_route_id
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.edge.caddy import CaddyEdge

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_ORG_PREFIX = f"aegis-org-{_ORG}-"


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


def _mock_edge() -> mock.MagicMock:
    edge = mock.MagicMock(spec=CaddyEdge)
    edge.list_routes.return_value = [
        {
            "@id": f"{_ORG_PREFIX}app-example-com",
            "match": [{"host": ["app.example.com"]}],
            "handle": [],
            "terminal": True,
        },
        {
            "@id": "aegis-org-other-domain",
            "match": [{"host": ["other.example.com"]}],
            "handle": [],
            "terminal": True,
        },
    ]
    result = mock.MagicMock()
    result.model_dump.return_value = {"@id": f"{_ORG_PREFIX}new-route", "status": "ok"}
    edge.add_route.return_value = result.model_dump.return_value
    return edge


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(edge_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with (
        mock.patch("aegis.server.api.routers.edge.get_caddy_edge", return_value=_mock_edge()),
        TestClient(fa, raise_server_exceptions=False) as c,
    ):
        yield c


# ── unit: prefix helpers ───────────────────────────────────────────────────────


def test_org_prefix_contains_org_id() -> None:
    prefix = _org_prefix(_ORG)
    assert str(_ORG) in prefix
    assert prefix.startswith("aegis-org-")


def test_org_route_id_is_org_prefixed() -> None:
    route_id = _org_route_id(_ORG, "app.example.com")
    assert route_id.startswith(_org_prefix(_ORG))
    assert "example" in route_id


def test_org_route_id_slugifies_domain() -> None:
    route_id = _org_route_id(_ORG, "My.App.Example.COM")
    assert " " not in route_id
    assert route_id == route_id.lower()


# ── GET /edge/routes — list filtered to caller's org ──────────────────────────


def test_list_routes_returns_only_caller_org(client: TestClient) -> None:
    r = client.get(f"/api/v1/orgs/{_ORG}/edge/routes")
    assert r.status_code == 200
    routes = r.json()
    assert len(routes) == 1
    assert routes[0]["@id"].startswith(_ORG_PREFIX)


def test_list_routes_excludes_other_org_routes(client: TestClient) -> None:
    r = client.get(f"/api/v1/orgs/{_ORG}/edge/routes")
    ids = [rt["@id"] for rt in r.json()]
    assert all(i.startswith(_ORG_PREFIX) for i in ids)


def test_list_routes_503_when_edge_not_init() -> None:
    fa = FastAPI()
    fa.include_router(edge_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with (
        mock.patch("aegis.server.api.routers.edge.get_caddy_edge", return_value=None),
        TestClient(fa, raise_server_exceptions=False) as c,
    ):
        r = c.get(f"/api/v1/orgs/{_ORG}/edge/routes")
    assert r.status_code == 503


# ── POST /edge/routes — add route with org-namespaced id ──────────────────────


def test_add_route_uses_org_namespaced_id(client: TestClient) -> None:
    with mock.patch("aegis.server.api.routers.edge.get_caddy_edge") as mock_get:
        edge = _mock_edge()
        edge.add_route.return_value = {"@id": f"{_ORG_PREFIX}app-example-com", "status": "ok"}
        mock_get.return_value = edge

        r = client.post(
            f"/api/v1/orgs/{_ORG}/edge/routes",
            json={"domain": "app.example.com", "upstream": "localhost:3000"},
        )
    assert r.status_code == 201
    expected_id = _org_route_id(_ORG, "app.example.com")
    edge.add_route.assert_called_once_with(
        "app.example.com", "localhost:3000", route_id=expected_id, service_url=""
    )


# ── DELETE /edge/routes/{id} — cross-org rejection ────────────────────────────


def test_remove_route_ok_with_own_org_prefix(client: TestClient) -> None:
    own_id = f"{_ORG_PREFIX}app-example-com"
    with mock.patch("aegis.server.api.routers.edge.get_caddy_edge") as mock_get:
        edge = _mock_edge()
        edge.remove_route.return_value = None
        mock_get.return_value = edge
        r = client.delete(f"/api/v1/orgs/{_ORG}/edge/routes/{own_id}")
    assert r.status_code == 204


def test_remove_route_403_on_cross_org_prefix(client: TestClient) -> None:
    other_prefix = _org_prefix(_OTHER_ORG)
    foreign_id = f"{other_prefix}foreign-route"
    r = client.delete(f"/api/v1/orgs/{_ORG}/edge/routes/{foreign_id}")
    assert r.status_code == 403


def test_remove_route_403_on_unprefixed_id(client: TestClient) -> None:
    r = client.delete(f"/api/v1/orgs/{_ORG}/edge/routes/bare-route-id")
    assert r.status_code == 403
