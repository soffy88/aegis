"""Tests for aegis.server.edge.caddy — CaddyEdge wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aegis.server.edge.caddy import CaddyEdge, _build_route, get_caddy_edge, init_caddy_edge
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: object) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[call-arg]


# ── _build_route ───────────────────────────────────────────────────────────────


def test_build_route_sets_host_match() -> None:
    r = _build_route("app.example.com", "localhost:3000")
    assert r["match"][0]["host"] == ["app.example.com"]


def test_build_route_sets_upstream_dial() -> None:
    r = _build_route("app.example.com", "localhost:3000")
    handler = r["handle"][0]
    assert handler["handler"] == "reverse_proxy"
    assert handler["upstreams"][0]["dial"] == "localhost:3000"


def test_build_route_auto_generates_id() -> None:
    r = _build_route("app.example.com", "localhost:3000")
    assert r["@id"].startswith("aegis-")
    assert "example" in r["@id"]


def test_build_route_explicit_id() -> None:
    r = _build_route("app.example.com", "localhost:3000", route_id="my-route")
    assert r["@id"] == "my-route"


def test_build_route_is_terminal() -> None:
    r = _build_route("x.com", "localhost:80")
    assert r["terminal"] is True


# ── CaddyEdge.from_config ──────────────────────────────────────────────────────


def test_from_config_uses_caddy_admin_url() -> None:
    cfg = _cfg()
    edge = CaddyEdge.from_config(cfg)
    assert edge._admin_url == cfg.caddy_admin_url


# ── add_route ──────────────────────────────────────────────────────────────────


def test_add_route_calls_oskill_caddy_route_add() -> None:
    mock_result = MagicMock()
    mock_result.status = "ok"
    mock_result.model_dump.return_value = {"status": "ok", "health_check_passed": True}

    edge = CaddyEdge(admin_url="http://localhost:2019")
    with patch("aegis.server.edge.caddy.caddy_route_add", return_value=mock_result) as mock_add:
        result = edge.add_route("app.test", "localhost:3000")
    mock_add.assert_called_once()
    call_kwargs = mock_add.call_args.kwargs
    assert call_kwargs["admin_url"] == "http://localhost:2019"
    assert call_kwargs["route"]["match"][0]["host"] == ["app.test"]
    assert call_kwargs["service_url"] == "http://localhost:3000"
    assert result["status"] == "ok"


def test_add_route_uses_explicit_service_url() -> None:
    mock_result = MagicMock()
    mock_result.model_dump.return_value = {}

    edge = CaddyEdge(admin_url="http://localhost:2019")
    with patch("aegis.server.edge.caddy.caddy_route_add", return_value=mock_result) as mock_add:
        edge.add_route("app.test", "localhost:3000", service_url="http://internal:8080/health")
    assert mock_add.call_args.kwargs["service_url"] == "http://internal:8080/health"


# ── remove_route ───────────────────────────────────────────────────────────────


def test_remove_route_calls_oprim_remove_atomic() -> None:
    edge = CaddyEdge(admin_url="http://localhost:2019")
    with patch("aegis.server.edge.caddy.caddy_route_remove_atomic", return_value={}) as mock_rm:
        edge.remove_route("aegis-app-test")
    mock_rm.assert_called_once_with(
        admin_url="http://localhost:2019",
        server_name="srv0",
        route_id="aegis-app-test",
        timeout_sec=10,
    )


# ── reload ─────────────────────────────────────────────────────────────────────


def test_reload_calls_oprim_admin_reload() -> None:
    mock_result = MagicMock()
    mock_result.model_dump.return_value = {"status": "reloaded"}

    edge = CaddyEdge(admin_url="http://localhost:2019")
    new_cfg = {"apps": {"http": {"servers": {}}}}
    with patch("aegis.server.edge.caddy.caddy_admin_reload", return_value=mock_result) as mock_rl:
        result = edge.reload(new_cfg)
    mock_rl.assert_called_once_with(
        admin_url="http://localhost:2019",
        new_config=new_cfg,
        timeout_sec=10,
    )
    assert result == {"status": "reloaded"}


# ── list_routes ────────────────────────────────────────────────────────────────


def test_list_routes_calls_oprim_routes_list() -> None:
    mock_route = MagicMock()
    mock_route.model_dump.return_value = {"@id": "r1"}

    edge = CaddyEdge(admin_url="http://localhost:2019")
    with patch("aegis.server.edge.caddy.caddy_routes_list", return_value=[mock_route]) as mock_ls:
        routes = edge.list_routes()
    mock_ls.assert_called_once_with(admin_url="http://localhost:2019", server_name="srv0")
    assert routes == [{"@id": "r1"}]


# ── singleton ──────────────────────────────────────────────────────────────────


def test_init_caddy_edge_sets_singleton() -> None:
    cfg = _cfg()
    edge = init_caddy_edge(cfg)
    assert get_caddy_edge() is edge
    assert isinstance(edge, CaddyEdge)
