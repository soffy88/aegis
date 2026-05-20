"""FastAPI application factory."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aegis.server.api.routers import alerts, events, health
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
    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(alerts.router)

    return app


app = create_app()
