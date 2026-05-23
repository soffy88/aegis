"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aegis.server.api.routers import alerts, domains, events, health
from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers import projects as projects_router
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.api.routers import store as store_router
from aegis.server.persistence import (
    apply_migrations,
    close_pool,
    get_pool,
    init_pool,
)
from aegis.server.runtime.config import AegisSettings
from aegis.server.runtime.logging import setup_logging

log = logging.getLogger(__name__)


def create_app(settings: AegisSettings | None = None) -> FastAPI:
    """Application factory.

    Args:
        settings: Optional pre-built settings (for testing).
    """
    cfg = settings or AegisSettings()
    setup_logging(cfg.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("aegis_starting host=%s port=%d", cfg.host, cfg.port)
        try:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            log.error("cannot create data_dir=%s: %s — aborting", cfg.data_dir, exc)
            raise SystemExit(1) from exc
        try:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            log.error("cannot create log_dir=%s: %s — aborting", cfg.log_dir, exc)
            raise SystemExit(1) from exc
        try:
            cfg.caddy_config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            log.error("cannot create caddy_config_dir=%s: %s — aborting", cfg.caddy_config_dir, exc)
            raise SystemExit(1) from exc
        await init_pool(
            dsn=cfg.postgres_dsn,
            min_size=cfg.postgres_pool_min,
            max_size=cfg.postgres_pool_max,
        )
        async with get_pool().acquire() as conn:
            n = await apply_migrations(conn)
            if n:
                log.info("applied %d migrations", n)

        yield

        log.info("aegis_shutting_down")
        await close_pool()

    app = FastAPI(
        title="Aegis",
        version="0.1.0",
        description="AI-powered self-hosted PaaS",
        lifespan=lifespan,
    )
    _cors_origins = os.environ.get("AEGIS_CORS_ORIGINS", "http://localhost:3010").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(alerts.router)
    app.include_router(docker_router.router)
    app.include_router(apps_router.router)
    app.include_router(domains.router)
    app.include_router(projects_router.router)
    app.include_router(store_router.router)
    app.include_router(runbooks_router.router)

    return app


app = create_app()
