"""Node model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# Liveness thresholds (seconds since last heartbeat).
_STALE_AFTER_SEC = 90
_OFFLINE_AFTER_SEC = 300


def derive_node_status(last_seen: datetime | None, *, now: datetime | None = None) -> str:
    """online (<90s) / stale (<300s) / offline (older or never seen)."""
    if last_seen is None:
        return "offline"
    now = now or datetime.now(UTC)
    age = (now - last_seen).total_seconds()
    if age < _STALE_AFTER_SEC:
        return "online"
    if age < _OFFLINE_AFTER_SEC:
        return "stale"
    return "offline"


@dataclass
class Node:
    node_id: UUID
    org_id: UUID
    host: str
    node_label: str
    docker_mode: str
    docker_host_url: str | None
    server_version: str | None
    os: str | None
    arch: str | None
    cpus: int | None
    memory_bytes: int | None
    registered_at: datetime
    last_seen: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Node:
        return cls(
            node_id=row["node_id"],
            org_id=row["org_id"],
            host=row["host"],
            node_label=row["node_label"],
            docker_mode=row["docker_mode"],
            docker_host_url=row["docker_host_url"],
            server_version=row["server_version"],
            os=row["os"],
            arch=row["arch"],
            cpus=row["cpus"],
            memory_bytes=row["memory_bytes"],
            registered_at=row["registered_at"],
            last_seen=row["last_seen"] if "last_seen" in row else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": str(self.node_id),
            "org_id": str(self.org_id),
            "host": self.host,
            "node_label": self.node_label,
            "docker_mode": self.docker_mode,
            "docker_host_url": self.docker_host_url,
            "server_version": self.server_version,
            "os": self.os,
            "arch": self.arch,
            "cpus": self.cpus,
            "memory_bytes": self.memory_bytes,
            "registered_at": self.registered_at.isoformat(),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "status": derive_node_status(self.last_seen),
        }
