"""Project discovery via Docker labels (走 oprim).

Convention:
  aegis.project=<name>       — required, project name
  aegis.health.path=/health  — optional, default /health
  aegis.health.port=8080     — optional, default from container ports
  aegis.role=primary|worker  — optional
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from oprim import docker_container_list

log = logging.getLogger(__name__)

_discovery_cache: tuple[list[dict[str, Any]], float] | None = None
_CACHE_TTL = 30.0


@dataclass
class DiscoveredContainer:
    id: str
    name: str
    image: str
    status: str
    ports: list[dict[str, Any]] = field(default_factory=list)
    health_path: str = "/health"
    health_port: int | None = None
    role: str | None = None


@dataclass
class DiscoveredProject:
    name: str
    containers: list[DiscoveredContainer] = field(default_factory=list)
    discovered_at: float = field(default_factory=time.time)

    @property
    def health_url(self) -> str | None:
        """Build health URL from primary container."""
        for c in self.containers:
            port = c.health_port or _infer_port(c.ports)
            if port:
                return f"http://localhost:{port}{c.health_path}"
        return None


def _infer_port(ports: list[dict[str, Any]]) -> int | None:
    """Infer host port from container port bindings."""
    for p in ports:
        host_port = p.get("HostPort") or p.get("PublicPort")
        if host_port:
            return int(host_port)
    return None


def discover_projects() -> list[DiscoveredProject]:
    """Discover projects from Docker containers with aegis.project label."""
    global _discovery_cache  # noqa: PLW0603

    now = time.monotonic()
    if _discovery_cache and (now - _discovery_cache[1]) < _CACHE_TTL:
        return [_dict_to_project(d) for d in _discovery_cache[0]]

    try:
        containers = docker_container_list(all=True, filters={"label": ["aegis.project"]})
    except Exception as exc:
        log.warning("Docker unavailable for project discovery: %s", exc)
        return []

    projects: dict[str, DiscoveredProject] = {}

    for info in containers:
        labels = info.labels or {}
        project_name = labels.get("aegis.project", "")
        if not project_name:
            continue

        if project_name not in projects:
            projects[project_name] = DiscoveredProject(name=project_name)

        dc = DiscoveredContainer(
            id=info.container_id[:12],
            name=info.name,
            image=info.image,
            status=info.status,
            ports=info.ports,
            health_path=labels.get("aegis.health.path", "/health"),
            health_port=int(labels["aegis.health.port"]) if "aegis.health.port" in labels else None,
            role=labels.get("aegis.role"),
        )
        projects[project_name].containers.append(dc)

    result = list(projects.values())
    _discovery_cache = ([_project_to_dict(p) for p in result], now)
    return result


def _project_to_dict(p: DiscoveredProject) -> dict[str, Any]:
    return {
        "name": p.name,
        "containers": [
            {
                "id": c.id,
                "name": c.name,
                "image": c.image,
                "status": c.status,
                "ports": c.ports,
                "health_path": c.health_path,
                "health_port": c.health_port,
                "role": c.role,
            }
            for c in p.containers
        ],
        "discovered_at": p.discovered_at,
    }


def _dict_to_project(d: dict[str, Any]) -> DiscoveredProject:
    return DiscoveredProject(
        name=d["name"],
        containers=[DiscoveredContainer(**c) for c in d["containers"]],
        discovered_at=d["discovered_at"],
    )
