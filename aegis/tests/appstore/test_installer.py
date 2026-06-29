"""Tests for aegis.server.appstore.installer — S2 AppInstaller assembly."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.appstore.installer import (
    _make_caddy_route_add_wrapper,
    _make_catalog_fetch_wrapper,
    _make_compose_pull_wrapper,
    _make_compose_up_wrapper,
    _make_verify_health_wrapper,
    build_app_installer,
    get_app_installer,
    init_app_installer,
    install_app,
)
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: Any) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[arg-type]


# ── build / singleton ─────────────────────────────────────────────────────────


def test_build_app_installer_returns_engine() -> None:
    svc = build_app_installer(_cfg())
    assert hasattr(svc, "run")
    assert hasattr(svc, "health")
    assert hasattr(svc, "submit_install")


def test_init_app_installer_sets_singleton() -> None:
    svc = init_app_installer(_cfg())
    assert get_app_installer() is svc


# ── catalog_fetch wrapper ─────────────────────────────────────────────────────


def test_catalog_fetch_wrapper_raises_when_no_url() -> None:
    wrapper = _make_catalog_fetch_wrapper(_cfg())
    with pytest.raises(RuntimeError, match="appstore_catalog_url not configured"):
        wrapper(app_id="my-app")


def test_catalog_fetch_wrapper_calls_oprim_and_returns_dict() -> None:
    entry_mock = MagicMock()
    entry_mock.model_dump.return_value = {
        "app_id": "test-app",
        "compose_file": "docker-compose.yml",
        "env_vars": {},
        "routes": [],
        "service_url": "http://localhost:8090",
    }
    cfg = _cfg(appstore_catalog_url="https://catalog.example.com")
    wrapper = _make_catalog_fetch_wrapper(cfg)
    with patch(
        "aegis.server.appstore.installer.appstore_catalog_fetch", return_value=entry_mock
    ) as mock_fn:
        result = wrapper(app_id="test-app")
    mock_fn.assert_called_once_with(catalog_url="https://catalog.example.com", app_id="test-app")
    assert result["app_id"] == "test-app"
    assert "compose_file" in result


# ── compose_pull wrapper ──────────────────────────────────────────────────────


def test_compose_pull_wrapper_calls_docker_compose_pull() -> None:
    cfg = _cfg()
    wrapper = _make_compose_pull_wrapper(cfg)
    with patch(
        "aegis.server.appstore.installer.docker_compose_pull", return_value={"status": "ok"}
    ) as mock_fn:
        result = wrapper(compose_file="docker-compose.yml")
    mock_fn.assert_called_once_with(compose_file="docker-compose.yml", docker_host=cfg.docker_host)
    assert result == {"status": "ok"}


# ── compose_up wrapper ────────────────────────────────────────────────────────


def test_compose_up_wrapper_calls_oprim_compose_up() -> None:
    cfg = _cfg()
    wrapper = _make_compose_up_wrapper(cfg)
    with patch(
        "aegis.server.appstore.installer.oprim_compose_up", return_value={"status": "started"}
    ) as mock_fn:
        result = wrapper(compose_file="docker-compose.yml", env={})
    mock_fn.assert_called_once_with(
        compose_file="docker-compose.yml", docker_host=cfg.docker_host, detach=True
    )
    assert result == {"status": "started"}


def test_compose_up_wrapper_writes_env_file_when_env_present(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """BACKLOG-075 resolved: env vars are written to a temp .env file next to the
    compose file (no longer silently dropped / warned about)."""
    import logging

    cfg = _cfg()
    wrapper = _make_compose_up_wrapper(cfg)
    compose_file = str(tmp_path / "docker-compose.yml")  # type: ignore[operator]
    with (
        patch(
            "aegis.server.appstore.installer.oprim_compose_up", return_value={"ok": True}
        ) as mock_up,
        caplog.at_level(logging.INFO, logger="aegis.server.appstore.installer"),
    ):
        result = wrapper(compose_file=compose_file, env={"KEY": "val"})

    assert result == {"ok": True}
    mock_up.assert_called_once()
    assert any("wrote 1 env vars" in r.getMessage() for r in caplog.records)


# ── caddy_route_add wrapper ───────────────────────────────────────────────────


def test_caddy_route_add_wrapper_iterates_routes() -> None:
    cfg = _cfg()
    wrapper = _make_caddy_route_add_wrapper(cfg)
    routes = [
        {"service_url": "http://svc1:8080"},
        {"service_url": "http://svc2:9090"},
    ]
    with patch(
        "aegis.server.appstore.installer.oskill_caddy_route_add", return_value=MagicMock()
    ) as mock_fn:
        results = wrapper(routes=routes)
    assert mock_fn.call_count == 2
    assert len(results) == 2


def test_caddy_route_add_wrapper_empty_routes_returns_empty() -> None:
    cfg = _cfg()
    wrapper = _make_caddy_route_add_wrapper(cfg)
    results = wrapper(routes=[])
    assert results == []


# ── verify_health wrapper ─────────────────────────────────────────────────────


def test_verify_health_wrapper_calls_oskill() -> None:
    wrapper = _make_verify_health_wrapper()
    with patch(
        "aegis.server.appstore.installer.verify_health_after_action", return_value=True
    ) as mock_fn:
        result = wrapper(service_url="http://svc:8080/health", retries=3)
    mock_fn.assert_called_once_with(service_url="http://svc:8080/health", retries=3)
    assert result is True


def test_verify_health_wrapper_true_when_no_url() -> None:
    wrapper = _make_verify_health_wrapper()
    result = wrapper(service_url="", retries=3)
    assert result is True


# ── install_app API ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_app_returns_none_when_no_service() -> None:
    with patch("aegis.server.appstore.installer._app_installer", None):
        result = await install_app("my-app")
    assert result is None


@pytest.mark.asyncio
async def test_install_app_calls_install_app_internal() -> None:
    mock_engine = MagicMock()
    mock_engine._install_app = AsyncMock(
        return_value={"status": "installed", "app_id": "my-app", "trail": []}
    )
    with patch("aegis.server.appstore.installer._app_installer", mock_engine):
        result = await install_app("my-app")
    mock_engine._install_app.assert_awaited_once_with({"app_id": "my-app"})
    assert result is not None
    assert result["status"] == "installed"
