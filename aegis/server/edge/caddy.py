"""Caddy edge service — thin wrapper over oprim/oskill Caddy primitives.

add_route  → oskill.caddy_route_add  (atomic add + health-check)
remove_route → oprim.caddy_route_remove_atomic  (atomic remove by route_id)
reload     → oprim.caddy_admin_reload  (full-config atomic reload)
list_routes → oprim.caddy_routes_list  (enumerate current routes)

All calls use cfg.caddy_admin_url and cfg.docker_host from AegisSettings.
Design: AEGIS_DESIGN v1.1.0 §8.5
"""

from __future__ import annotations

import logging
from typing import Any

from oprim import caddy_admin_reload, caddy_route_remove_atomic, caddy_routes_list
from oskill import caddy_route_add

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)


def _build_route(domain: str, upstream: str, route_id: str | None = None) -> dict[str, Any]:
    """Build a minimal Caddy route dict for a domain → upstream proxy."""
    rid = route_id or f"aegis-{domain.replace('.', '-')}"
    return {
        "@id": rid,
        "match": [{"host": [domain]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
            }
        ],
        "terminal": True,
    }


class CaddyEdge:
    """Wrapper around oprim/oskill Caddy primitives.

    Use `from_config(cfg)` to create from AegisSettings.
    """

    def __init__(
        self,
        admin_url: str,
        server_name: str = "srv0",
        health_retries: int = 3,
        timeout_sec: int = 10,
    ) -> None:
        self._admin_url = admin_url
        self._server_name = server_name
        self._health_retries = health_retries
        self._timeout_sec = timeout_sec

    @classmethod
    def from_config(cls, cfg: AegisSettings) -> CaddyEdge:
        return cls(admin_url=cfg.caddy_admin_url)

    def add_route(
        self,
        domain: str,
        upstream: str,
        *,
        route_id: str | None = None,
        service_url: str = "",
    ) -> dict[str, Any]:
        """Add a domain → upstream route atomically + verify service health.

        Args:
            domain:     Public hostname (e.g. "app.example.com").
            upstream:   Internal dial address (e.g. "localhost:3000").
            route_id:   Explicit Caddy route @id; auto-generated if None.
            service_url: URL for health-check (e.g. "http://localhost:3000").
                         Defaults to http://{upstream} if empty.

        Returns:
            oskill.CaddyRouteAddResult as dict.
        """
        route = _build_route(domain, upstream, route_id)
        svc_url = service_url or f"http://{upstream}"
        result = caddy_route_add(
            admin_url=self._admin_url,
            route=route,
            service_url=svc_url,
            server_name=self._server_name,
            health_retries=self._health_retries,
            timeout_sec=self._timeout_sec,
        )
        log.info(
            "caddy_route_added domain=%s upstream=%s status=%s", domain, upstream, result.status
        )
        return result.model_dump()

    def remove_route(self, route_id: str) -> dict[str, Any]:
        """Remove a route by its Caddy @id.

        Args:
            route_id: The Caddy route @id (e.g. "aegis-app-example-com").

        Returns:
            oprim.caddy_route_remove_atomic result dict.
        """
        result = caddy_route_remove_atomic(
            admin_url=self._admin_url,
            server_name=self._server_name,
            route_id=route_id,
            timeout_sec=self._timeout_sec,
        )
        log.info("caddy_route_removed route_id=%s", route_id)
        return result  # type: ignore[return-value]

    def reload(self, new_config: dict[str, Any]) -> dict[str, Any]:
        """Replace the entire Caddy config atomically.

        Args:
            new_config: Full Caddy JSON config dict.

        Returns:
            oprim.caddy_admin_reload result dict.
        """
        result = caddy_admin_reload(
            admin_url=self._admin_url,
            new_config=new_config,
            timeout_sec=self._timeout_sec,
        )
        log.info("caddy_config_reloaded admin_url=%s", self._admin_url)
        return result.model_dump()  # type: ignore[union-attr]

    def list_routes(self) -> list[dict[str, Any]]:
        """Return all current Caddy routes as a list of dicts."""
        routes = caddy_routes_list(
            admin_url=self._admin_url,
            server_name=self._server_name,
        )
        return [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in routes]


# ── module-level singleton ─────────────────────────────────────────────────────

_caddy_edge: CaddyEdge | None = None


def get_caddy_edge() -> CaddyEdge | None:
    return _caddy_edge


def init_caddy_edge(cfg: AegisSettings) -> CaddyEdge:
    global _caddy_edge
    _caddy_edge = CaddyEdge.from_config(cfg)
    return _caddy_edge
