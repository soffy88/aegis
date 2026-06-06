"""Metric collection via oprim primitives."""

from __future__ import annotations

import logging
from typing import Any

from oprim import docker_stats, fs_disk_usage, system_cpu_usage, system_ram_usage

log = logging.getLogger(__name__)


def _safe(fn: Any, **kwargs: Any) -> Any:
    """Call fn(**kwargs), return None on any exception."""
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("collector_error fn=%s: %s", getattr(fn, "__name__", fn), exc)
        return None


def collect_metrics(docker_host: str = "unix:///var/run/docker.sock") -> list[dict[str, Any]]:
    """Collect host and container metrics using oprim.

    Returns a list of metric point dicts compatible with MetricPoint schema:
        {"name": str, "value": float, "unit": str, "tags": dict}
    """
    points: list[dict[str, Any]] = []

    cpu = _safe(system_cpu_usage)
    if cpu is not None:
        cpu_val = getattr(cpu, "cpu_percent", None) or getattr(cpu, "percent", None)
        if cpu_val is not None:
            points.append({"name": "cpu_percent", "value": float(cpu_val), "unit": "%", "tags": {}})

    ram = _safe(system_ram_usage)
    if ram is not None:
        ram_val = getattr(ram, "ram_percent", None) or getattr(ram, "percent", None)
        if ram_val is not None:
            points.append({"name": "ram_percent", "value": float(ram_val), "unit": "%", "tags": {}})

    disk = _safe(fs_disk_usage, path="/")
    if disk is not None:
        disk_val = getattr(disk, "disk_percent", None) or getattr(disk, "percent", None)
        if disk_val is not None:
            points.append(
                {
                    "name": "disk_percent",
                    "value": float(disk_val),
                    "unit": "%",
                    "tags": {"path": "/"},
                }
            )

    stats = _safe(docker_stats, docker_host=docker_host)
    if stats is not None:
        containers = stats if isinstance(stats, list) else getattr(stats, "containers", [])
        for c in containers:
            name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
            cpu_pct = getattr(c, "cpu_percent", None) or (
                c.get("cpu_percent") if isinstance(c, dict) else None
            )
            if name and cpu_pct is not None:
                points.append(
                    {
                        "name": "docker_cpu_percent",
                        "value": float(cpu_pct),
                        "unit": "%",
                        "tags": {"container": str(name)},
                    }
                )

    log.debug("collected %d metric points", len(points))
    return points
