"""Tests for aegis-plugins — real plugins + stub discovery via entry_points."""

from __future__ import annotations

import importlib.metadata
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_autoheal_sdk import ActionResultStatus, AutoHealPlugin, Severity
from aegis_plugins.plugins.cleanup_disk import CleanupDiskPlugin
from aegis_plugins.plugins.drain_node import DrainNodePlugin
from aegis_plugins.plugins.notify_oncall import NotifyOncallPlugin
from aegis_plugins.plugins.restart_service import RestartServicePlugin
from aegis_plugins.plugins.rotate_credentials import RotateCredentialsPlugin
from aegis_plugins.plugins.scale_down import ScaleDownPlugin
from aegis_plugins.plugins.stubs import (
    CompactDbPlugin,
    KillProcessPlugin,
    RotateLogsPlugin,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_ctx(
    health: str = "down",
    payload: dict[str, Any] | None = None,
    env: Severity = Severity.STAGING,
) -> MagicMock:
    ctx = MagicMock()
    ctx.service.name = "test-svc"
    ctx.service.health = health
    ctx.org_environment = env
    ctx.trace_id = "trace-001"
    ctx.alert_payload = payload or {}
    ctx.docker_restart = AsyncMock()
    ctx.http_get = AsyncMock(return_value={"status_code": 200, "body": {}})
    ctx.alert_human = AsyncMock()
    ctx.emit_trail_event = AsyncMock()
    return ctx


# ── entry_points discovery ─────────────────────────────────────────────────────


def test_entry_points_register_30_plugins() -> None:
    eps = list(importlib.metadata.entry_points(group="aegis.plugins"))
    assert len(eps) == 30, f"expected 30 entry points, got {len(eps)}: {[e.name for e in eps]}"


def test_entry_points_all_load_successfully() -> None:
    eps = importlib.metadata.entry_points(group="aegis.plugins")
    for ep in eps:
        cls = ep.load()
        assert issubclass(cls, AutoHealPlugin), f"{ep.name} is not an AutoHealPlugin subclass"


def test_entry_points_names_are_slugs() -> None:
    eps = importlib.metadata.entry_points(group="aegis.plugins")
    for ep in eps:
        assert ep.name.replace("-", "").isalnum(), f"entry point name not slug: {ep.name!r}"


# ── plugin validate_config ─────────────────────────────────────────────────────


def test_real_plugins_pass_validate_config() -> None:
    real_plugins = [
        RestartServicePlugin,
        ScaleDownPlugin,
        DrainNodePlugin,
        RotateCredentialsPlugin,
        NotifyOncallPlugin,
        CleanupDiskPlugin,
    ]
    for cls in real_plugins:
        cls.validate_config()  # must not raise


def test_stub_plugins_pass_validate_config() -> None:
    for cls in [KillProcessPlugin, CompactDbPlugin, RotateLogsPlugin]:
        cls.validate_config()


# ── Case 1: restart-service ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_service_pre_check_true_when_down() -> None:
    ctx = _make_ctx(health="down")
    assert await RestartServicePlugin().pre_check(ctx) is True


@pytest.mark.asyncio
async def test_restart_service_pre_check_false_when_healthy() -> None:
    ctx = _make_ctx(health="healthy")
    assert await RestartServicePlugin().pre_check(ctx) is False


@pytest.mark.asyncio
async def test_restart_service_execute_calls_docker_restart() -> None:
    ctx = _make_ctx(health="down", payload={"service_name": "worker"})
    result = await RestartServicePlugin().execute(ctx)
    ctx.docker_restart.assert_called_once_with("worker")
    assert result.is_success


@pytest.mark.asyncio
async def test_restart_service_rollback_escalates() -> None:
    ctx = _make_ctx()
    result = await RestartServicePlugin().rollback(ctx)
    assert result.status == ActionResultStatus.ESCALATE
    assert result.escalate_to == "human"


# ── Case 2: scale-down ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scale_down_pre_check_false_when_one_replica() -> None:
    ctx = _make_ctx(payload={"current_replicas": 1})
    assert await ScaleDownPlugin().pre_check(ctx) is False


@pytest.mark.asyncio
async def test_scale_down_pre_check_true_when_multiple_replicas() -> None:
    ctx = _make_ctx(payload={"current_replicas": 3})
    assert await ScaleDownPlugin().pre_check(ctx) is True


@pytest.mark.asyncio
async def test_scale_down_execute_calls_docker_api() -> None:
    ctx = _make_ctx(
        payload={
            "docker_api_url": "http://docker:2375",
            "service_id": "web",
            "current_replicas": 3,
        }
    )
    result = await ScaleDownPlugin().execute(ctx)
    ctx.http_get.assert_called()
    assert result.is_success


@pytest.mark.asyncio
async def test_scale_down_requires_approval_for_critical() -> None:
    assert ScaleDownPlugin.requires_approval_when == Severity.PRODUCTION


# ── Case 3: notify-oncall ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_oncall_execute_calls_alert_human() -> None:
    ctx = _make_ctx(payload={"alert_name": "cpu_high"})
    result = await NotifyOncallPlugin().execute(ctx)
    ctx.alert_human.assert_called_once()
    assert result.is_success


@pytest.mark.asyncio
async def test_notify_oncall_rollback_skips() -> None:
    ctx = _make_ctx()
    result = await NotifyOncallPlugin().rollback(ctx)
    assert result.status == ActionResultStatus.SKIPPED


# ── Case 4: stubs always skip ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stub_pre_check_always_false() -> None:
    ctx = _make_ctx()
    for cls in [KillProcessPlugin, CompactDbPlugin, RotateLogsPlugin]:
        assert await cls().pre_check(ctx) is False


@pytest.mark.asyncio
async def test_stub_execute_returns_skipped() -> None:
    ctx = _make_ctx()
    for cls in [KillProcessPlugin, CompactDbPlugin]:
        result = await cls().execute(ctx)
        assert result.status == ActionResultStatus.SKIPPED


# ── Case 5: rotate-credentials requires approval ──────────────────────────────


def test_rotate_credentials_requires_approval_critical() -> None:
    assert RotateCredentialsPlugin.requires_approval_when == Severity.PRODUCTION


@pytest.mark.asyncio
async def test_rotate_credentials_pre_check_false_without_webhook() -> None:
    ctx = _make_ctx(payload={})
    assert await RotateCredentialsPlugin().pre_check(ctx) is False


@pytest.mark.asyncio
async def test_rotate_credentials_execute_calls_webhook() -> None:
    ctx = _make_ctx(payload={"rotation_webhook_url": "http://vault/rotate"})
    result = await RotateCredentialsPlugin().execute(ctx)
    ctx.http_get.assert_called()
    assert result.is_success


# ── Case 6: drain-node requires approval ──────────────────────────────────────


def test_drain_node_requires_approval_warning() -> None:
    assert DrainNodePlugin.requires_approval_when == Severity.STAGING


@pytest.mark.asyncio
async def test_drain_node_pre_check_false_without_node_id() -> None:
    ctx = _make_ctx(payload={"docker_api_url": "http://docker:2375"})
    assert await DrainNodePlugin().pre_check(ctx) is False
