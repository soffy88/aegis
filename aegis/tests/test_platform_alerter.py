"""Tests for platform_alerter — S1 BrainAlerter assembly (TDD RED→GREEN)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from aegis.server.alert.platform_alerter import (
    build_platform_alerter,
    disk_usage_evaluator,
    get_platform_alerter,
    init_platform_alerter,
    postgres_pool_evaluator,
    system_cpu_evaluator,
    system_ram_evaluator,
    telegram_channel,
)
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: object) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[arg-type]


# ── assembly / singleton ──────────────────────────────────────────────────────


def test_build_platform_alerter_returns_engine() -> None:
    svc = build_platform_alerter(_cfg())
    assert hasattr(svc, "run")
    assert hasattr(svc, "health")


def test_init_platform_alerter_sets_singleton() -> None:
    cfg = _cfg()
    svc = init_platform_alerter(cfg)
    assert get_platform_alerter() is svc


# ── postgres_pool_evaluator ───────────────────────────────────────────────────


def test_postgres_pool_evaluator_evaluator_error_on_connection_error() -> None:
    with patch("aegis.server.alert.platform_alerter.postgres_pool_status") as mock_fn:
        mock_fn.side_effect = Exception("connection refused")
        events = postgres_pool_evaluator(config={"dsn": "postgresql://localhost/test"})
    assert len(events) == 1
    assert events[0]["entity_id"] == "postgres_pool"
    assert events[0]["severity"] == "warning"
    assert events[0]["kind"] == "evaluator_error"
    assert "connection refused" in events[0]["message"]


def test_postgres_pool_evaluator_empty_when_healthy() -> None:
    pool_status = MagicMock()
    pool_status.usage_percent = 30.0
    with patch("aegis.server.alert.platform_alerter.postgres_pool_status") as mock_fn:
        mock_fn.return_value = pool_status
        events = postgres_pool_evaluator(
            config={"dsn": "postgresql://localhost/test", "pool_usage_threshold": 85.0}
        )
    assert events == []


def test_postgres_pool_evaluator_warning_when_usage_exceeds_threshold() -> None:
    pool_status = MagicMock()
    pool_status.usage_percent = 90.0
    with patch("aegis.server.alert.platform_alerter.postgres_pool_status") as mock_fn:
        mock_fn.return_value = pool_status
        events = postgres_pool_evaluator(
            config={"dsn": "postgresql://localhost/test", "pool_usage_threshold": 85.0}
        )
    assert len(events) == 1
    assert events[0]["severity"] in ("warning", "critical")
    assert events[0]["entity_id"] == "postgres_pool"


def test_postgres_pool_evaluator_critical_when_usage_above_95() -> None:
    pool_status = MagicMock()
    pool_status.usage_percent = 97.0
    with patch("aegis.server.alert.platform_alerter.postgres_pool_status") as mock_fn:
        mock_fn.return_value = pool_status
        events = postgres_pool_evaluator(
            config={"dsn": "postgresql://localhost/test", "pool_usage_threshold": 85.0}
        )
    assert events[0]["severity"] == "critical"


def test_postgres_pool_evaluator_empty_when_no_dsn() -> None:
    events = postgres_pool_evaluator(config={})
    assert events == []


# ── system_cpu_evaluator ──────────────────────────────────────────────────────


def test_system_cpu_evaluator_empty_below_threshold() -> None:
    with patch("aegis.server.alert.platform_alerter.system_cpu_usage") as mock_fn:
        mock_fn.return_value = 50.0
        events = system_cpu_evaluator(config={"threshold": 85.0})
    assert events == []


def test_system_cpu_evaluator_warning_above_threshold() -> None:
    with patch("aegis.server.alert.platform_alerter.system_cpu_usage") as mock_fn:
        mock_fn.return_value = 88.0
        events = system_cpu_evaluator(config={"threshold": 85.0})
    assert len(events) == 1
    assert events[0]["entity_id"] == "system_cpu"
    assert events[0]["severity"] == "warning"


def test_system_cpu_evaluator_critical_above_95() -> None:
    with patch("aegis.server.alert.platform_alerter.system_cpu_usage") as mock_fn:
        mock_fn.return_value = 96.0
        events = system_cpu_evaluator(config={"threshold": 85.0})
    assert events[0]["severity"] == "critical"


def test_system_cpu_evaluator_evaluator_error_on_exception() -> None:
    with patch("aegis.server.alert.platform_alerter.system_cpu_usage") as mock_fn:
        mock_fn.side_effect = RuntimeError("psutil unavailable")
        events = system_cpu_evaluator(config={})
    assert events[0]["severity"] == "warning"
    assert events[0]["kind"] == "evaluator_error"


# ── system_ram_evaluator ──────────────────────────────────────────────────────


def test_system_ram_evaluator_empty_below_threshold() -> None:
    with patch("aegis.server.alert.platform_alerter.system_ram_usage") as mock_fn:
        mock_fn.return_value = {
            "used_percent": 60.0,
            "total_bytes": 8_000_000_000,
            "used_bytes": 4_800_000_000,
            "available_bytes": 3_200_000_000,
        }
        events = system_ram_evaluator(config={"threshold": 90.0})
    assert events == []


def test_system_ram_evaluator_warning_above_threshold() -> None:
    with patch("aegis.server.alert.platform_alerter.system_ram_usage") as mock_fn:
        mock_fn.return_value = {
            "used_percent": 92.0,
            "total_bytes": 8_000_000_000,
            "used_bytes": 7_360_000_000,
            "available_bytes": 640_000_000,
        }
        events = system_ram_evaluator(config={"threshold": 90.0})
    assert len(events) == 1
    assert events[0]["entity_id"] == "system_ram"
    assert events[0]["severity"] in ("warning", "critical")


# ── disk_usage_evaluator ──────────────────────────────────────────────────────


def test_disk_usage_evaluator_empty_below_threshold() -> None:
    du = MagicMock()
    du.used_percent = 50.0
    with patch("aegis.server.alert.platform_alerter.fs_disk_usage") as mock_fn:
        mock_fn.return_value = du
        events = disk_usage_evaluator(config={"path": "/", "threshold": 85.0})
    assert events == []


def test_disk_usage_evaluator_warning_above_threshold() -> None:
    du = MagicMock()
    du.used_percent = 88.0
    with patch("aegis.server.alert.platform_alerter.fs_disk_usage") as mock_fn:
        mock_fn.return_value = du
        events = disk_usage_evaluator(config={"path": "/", "threshold": 85.0})
    assert len(events) == 1
    assert events[0]["entity_id"] == "disk_/"
    assert events[0]["severity"] == "warning"


def test_disk_usage_evaluator_critical_above_95() -> None:
    du = MagicMock()
    du.used_percent = 97.0
    with patch("aegis.server.alert.platform_alerter.fs_disk_usage") as mock_fn:
        mock_fn.return_value = du
        events = disk_usage_evaluator(config={"path": "/data", "threshold": 85.0})
    assert events[0]["severity"] == "critical"
    assert events[0]["entity_id"] == "disk_/data"


def test_disk_usage_evaluator_evaluator_error_on_exception() -> None:
    with patch("aegis.server.alert.platform_alerter.fs_disk_usage") as mock_fn:
        mock_fn.side_effect = OSError("no such path")
        events = disk_usage_evaluator(config={"path": "/missing"})
    assert events[0]["severity"] == "warning"
    assert events[0]["kind"] == "evaluator_error"


# ── telegram_channel ──────────────────────────────────────────────────────────


def test_telegram_channel_calls_telegram_send() -> None:
    from obase.notify import TelegramRequest

    with patch(
        "aegis.server.alert.platform_alerter.telegram_send", new_callable=MagicMock
    ) as mock_send:
        telegram_channel(text="alert text", chat_id="123456", bot_token="bot:token")
        mock_send.assert_called_once()
        req = mock_send.call_args[0][0]
        assert isinstance(req, TelegramRequest)
        assert req.text == "alert text"
        assert req.chat_id == "123456"
        assert req.bot_token == "bot:token"


def test_telegram_channel_skips_when_no_bot_token(caplog: pytest.LogCaptureFixture) -> None:
    with patch(
        "aegis.server.alert.platform_alerter.telegram_send", new_callable=MagicMock
    ) as mock_send:
        with caplog.at_level(logging.WARNING, logger="aegis.server.alert.platform_alerter"):
            telegram_channel(text="alert dropped", chat_id="123", bot_token="")
        mock_send.assert_not_called()
    assert any("bot_token empty" in r.message for r in caplog.records)


def test_telegram_channel_passes_extra_kwargs_silently() -> None:
    with patch(
        "aegis.server.alert.platform_alerter.telegram_send", new_callable=MagicMock
    ) as mock_send:
        telegram_channel(text="x", chat_id="1", bot_token="tok", unknown_kwarg="y")
        mock_send.assert_called_once()
