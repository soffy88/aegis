"""Tests for aegis_agent._collector.collect_metrics."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _make_cpu(pct: float) -> MagicMock:
    m = MagicMock()
    m.cpu_percent = pct
    return m


def _make_ram(pct: float) -> MagicMock:
    m = MagicMock()
    m.ram_percent = pct
    return m


def _make_disk(pct: float) -> MagicMock:
    m = MagicMock()
    m.disk_percent = pct
    return m


def _docker_container(name: str, cpu: float) -> MagicMock:
    c = MagicMock()
    c.name = name
    c.cpu_percent = cpu
    return c


def _patch_oprim(**kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "system_cpu_usage": _make_cpu(42.0),
        "system_ram_usage": _make_ram(65.0),
        "fs_disk_usage": _make_disk(30.0),
        "docker_stats": [],
    }
    defaults.update(kwargs)

    return patch.multiple(
        "aegis_agent._collector",
        system_cpu_usage=MagicMock(return_value=defaults["system_cpu_usage"]),
        system_ram_usage=MagicMock(return_value=defaults["system_ram_usage"]),
        fs_disk_usage=MagicMock(return_value=defaults["fs_disk_usage"]),
        docker_stats=MagicMock(return_value=defaults["docker_stats"]),
    )


async def test_collect_returns_cpu_metric() -> None:
    with _patch_oprim():
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()
    names = [p["name"] for p in points]
    assert "cpu_percent" in names
    cpu = next(p for p in points if p["name"] == "cpu_percent")
    assert cpu["value"] == 42.0
    assert cpu["unit"] == "%"


async def test_collect_returns_ram_metric() -> None:
    with _patch_oprim():
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()
    names = [p["name"] for p in points]
    assert "ram_percent" in names


async def test_collect_returns_disk_metric() -> None:
    with _patch_oprim():
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()
    disk = next((p for p in points if p["name"] == "disk_percent"), None)
    assert disk is not None
    assert disk["tags"]["path"] == "/"


async def test_collect_returns_docker_cpu_metrics() -> None:
    containers = [_docker_container("postgres", 5.5), _docker_container("redis", 1.2)]
    with _patch_oprim(docker_stats=containers):
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()
    docker_points = [p for p in points if p["name"] == "docker_cpu_percent"]
    assert len(docker_points) == 2
    names = {p["tags"]["container"] for p in docker_points}
    assert names == {"postgres", "redis"}


async def test_collect_skips_failed_oprim_call() -> None:
    """If an oprim call raises, it's skipped — other metrics still collected."""
    with (
        patch("aegis_agent._collector.system_cpu_usage", side_effect=RuntimeError("no perms")),
        patch(
            "aegis_agent._collector.system_ram_usage",
            return_value=_make_ram(70.0),
        ),
        patch(
            "aegis_agent._collector.fs_disk_usage",
            return_value=_make_disk(40.0),
        ),
        patch("aegis_agent._collector.docker_stats", return_value=[]),
    ):
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()

    names = [p["name"] for p in points]
    assert "cpu_percent" not in names
    assert "ram_percent" in names


async def test_collect_no_docker_when_stats_empty() -> None:
    with _patch_oprim(docker_stats=[]):
        from aegis_agent._collector import collect_metrics

        points = await collect_metrics()
    assert not any(p["name"] == "docker_cpu_percent" for p in points)


async def test_collect_returns_none_on_timeout() -> None:
    """A hanging oprim call is bounded by a timeout instead of hanging forever."""
    import time

    def _hang(**_kwargs: Any) -> None:
        time.sleep(0.2)

    with (
        patch("aegis_agent._collector.system_cpu_usage", side_effect=_hang),
        patch("aegis_agent._collector.system_ram_usage", return_value=_make_ram(70.0)),
        patch("aegis_agent._collector.fs_disk_usage", return_value=_make_disk(40.0)),
        patch("aegis_agent._collector.docker_stats", return_value=[]),
    ):
        from aegis_agent._collector import _safe, system_cpu_usage

        result = await _safe(system_cpu_usage, timeout=0.01)

    assert result is None
