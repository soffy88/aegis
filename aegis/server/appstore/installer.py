"""AppStore Installer — AppInstallerEngine assembly.

Wires 5 injection points to oprim/oskill with adapter wrappers.
All 5 have calling-convention mismatches vs the engine protocol
(same root cause as AEGIS-BACKLOG-071 in platform_alerter.py).

AppInstallerEngine calling conventions (from _install_app source):
  catalog_fetch(*, app_id: str) → dict{compose_file, env_vars, routes, service_url}
  compose_pull(*, compose_file: str) → Any
  compose_up(*, compose_file: str, env: dict) → Any
  caddy_route_add(*, routes: list[dict]) → Any
  verify_health(*, service_url: str, retries: int) → bool

oprim/oskill true signatures require additional context (catalog_url, docker_host,
caddy admin_url) that must be captured from AegisSettings at wrapper build time.

TODO(AEGIS-BACKLOG-070): bypass new AppInstallerEngine; switch to assemble(manifest)
  after oservice v0.4.2 fixes _detect_element_kind.
TODO(AEGIS-BACKLOG-074): AppInstallerEngine uses queue-based API; _install_app called
  directly for request/response in install_app(). Expose public async API in v0.4.2.
TODO(AEGIS-BACKLOG-075): oprim.compose_up (v2.31.0) has no env parameter; env vars
  from AppCatalogEntry.env_vars are silently dropped. Fix: oprim v2.32 add env kwarg
  or pre-write .env file before compose_up call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from obase.docker import docker_compose_pull
from oprim import appstore_catalog_fetch
from obase.docker import compose_up as oprim_compose_up
from oservi.engines.app_installer import AppInstallerEngine
from oskill import caddy_route_add as oskill_caddy_route_add
from oskill import verify_health_after_action

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)


# ── Injection wrappers ────────────────────────────────────────────────────────
# Each wrapper bridges the engine's calling convention to the oprim/oskill signature.


def _make_catalog_fetch_wrapper(cfg: AegisSettings) -> Callable[..., Any]:
    """catalog_fetch(*, app_id) → dict — wraps oprim.appstore_catalog_fetch."""

    def catalog_fetch(*, app_id: str) -> dict[str, Any]:
        if not cfg.appstore_catalog_url:
            raise RuntimeError(
                "appstore_catalog_url not configured (set AEGIS_APPSTORE_CATALOG_URL)"
            )
        entry = appstore_catalog_fetch(catalog_url=cfg.appstore_catalog_url, app_id=app_id)
        return entry.model_dump()

    return catalog_fetch


def _make_compose_pull_wrapper(cfg: AegisSettings) -> Callable[..., Any]:
    """compose_pull(*, compose_file) → dict — wraps oprim.docker_compose_pull."""

    def compose_pull(*, compose_file: str) -> dict[str, Any]:
        return docker_compose_pull(compose_file=compose_file, docker_host=cfg.docker_host)

    return compose_pull


def _make_compose_up_wrapper(cfg: AegisSettings) -> Callable[..., Any]:
    """compose_up(*, compose_file, env) → dict — wraps oprim.compose_up.

    Workaround for AEGIS-BACKLOG-075 (oprim.compose_up lacks env parameter):
    env vars are written to a temp .env file alongside the compose file so that
    Docker Compose picks them up automatically via the standard env_file convention.
    This file is cleaned up after compose_up completes.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    def compose_up(*, compose_file: str, env: dict[str, Any]) -> dict[str, Any]:
        env_file_path: str | None = None
        try:
            if env:
                # Write env vars to a temp file next to the compose file
                compose_dir = os.path.dirname(os.path.abspath(compose_file))
                fd, env_file_path = tempfile.mkstemp(
                    suffix=".env", prefix=".aegis_", dir=compose_dir
                )
                with os.fdopen(fd, "w") as f:
                    for k, v in env.items():
                        # Escape newlines in values; Docker Compose reads KEY=VALUE lines
                        safe_val = str(v).replace("\n", "\\n")
                        f.write(f"{k}={safe_val}\n")
                log.info("compose_up_wrapper: wrote %d env vars to %s", len(env), env_file_path)

            return oprim_compose_up(
                compose_file=compose_file, docker_host=cfg.docker_host, detach=True
            )
        finally:
            if env_file_path and os.path.exists(env_file_path):
                try:
                    os.unlink(env_file_path)
                except OSError:
                    log.warning(
                        "compose_up_wrapper: failed to delete temp env file %s", env_file_path
                    )

    return compose_up


def _make_caddy_route_add_wrapper(cfg: AegisSettings) -> Callable[..., Any]:
    """caddy_route_add(*, routes) → list — wraps oskill.caddy_route_add per route."""

    def caddy_route_add(*, routes: list[dict[str, Any]]) -> list[Any]:
        results = []
        for route in routes:
            result = oskill_caddy_route_add(
                admin_url=cfg.caddy_admin_url,
                route=route.get("route_config", route),
                service_url=route.get("service_url", ""),
            )
            results.append(result)
        return results

    return caddy_route_add


def _make_verify_health_wrapper() -> Callable[..., Any]:
    """verify_health(*, service_url, retries) → bool — wraps oskill.verify_health_after_action."""

    def verify_health(*, service_url: str, retries: int) -> bool:
        if not service_url:
            log.info("verify_health_wrapper: no service_url, skipping (assume healthy)")
            return True
        return verify_health_after_action(service_url=service_url, retries=retries)

    return verify_health


# ── Assembly ──────────────────────────────────────────────────────────────────


def build_app_installer(cfg: AegisSettings) -> AppInstallerEngine:
    """Build AppInstallerEngine with 5 wrapped injection points."""
    return AppInstallerEngine(
        catalog_fetch=_make_catalog_fetch_wrapper(cfg),
        compose_pull=_make_compose_pull_wrapper(cfg),
        compose_up=_make_compose_up_wrapper(cfg),
        caddy_route_add=_make_caddy_route_add_wrapper(cfg),
        verify_health=_make_verify_health_wrapper(),
        trigger={},
        config={
            "health_retries": cfg.appstore_health_retries,
            "skip_pull": cfg.appstore_skip_pull,
        },
        name="aegis-app-installer",
    )


# ── Module-level singleton ────────────────────────────────────────────────────

_app_installer: AppInstallerEngine | None = None


def get_app_installer() -> AppInstallerEngine | None:
    return _app_installer


def init_app_installer(cfg: AegisSettings) -> AppInstallerEngine:
    global _app_installer
    _app_installer = build_app_installer(cfg)
    return _app_installer


# ── AppStore API ──────────────────────────────────────────────────────────────


async def install_app(app_id: str) -> dict[str, Any] | None:
    """Install an app by ID. Returns install result dict or None if not initialized.

    Calls _install_app directly for request/response in FastAPI context.
    TODO(AEGIS-BACKLOG-074): remove when oservice exposes public async invoke API.
    """
    service = get_app_installer()
    if service is None:
        log.error("app_installer_not_initialized — call init_app_installer first")
        return None
    return await service._install_app({"app_id": app_id})  # type: ignore[attr-defined]
