"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from obase import ProviderRegistry

from aegis.server.api.routers import alert_fired, alert_rules, alerts, domains, events, health
from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import auth as auth_router
from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers import envelope as envelope_router
from aegis.server.api.routers import orgs as orgs_router
from aegis.server.api.routers import projects as projects_router
from aegis.server.api.routers import release_gates as release_gates_router
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.api.routers import store as store_router
from aegis.server.api.routers import users as users_router
from aegis.server.api.routers import webhook_subscriptions as webhook_subscriptions_router
from aegis.server.persistence import (
    apply_migrations,
    close_pool,
    get_pool,
    init_pool,
)
from aegis.server.runtime.config import AegisSettings
from aegis.server.runtime.logging import setup_logging

log = logging.getLogger(__name__)


def init_sentry_if_enabled() -> None:
    """C3-7: Aegis self-monitoring via sentry-python (dev/test only).

    Enabled only when AEGIS_SENTRY_ENABLED=true AND ENV != 'prod'.
    Prod is excluded to prevent a self-monitoring death loop if platform-postgres
    is down (Aegis can't write its own errors → SDK retries → more errors).
    """
    if os.environ.get("AEGIS_SENTRY_ENABLED") != "true":
        return
    if os.environ.get("ENV") == "prod":
        return
    dsn = os.environ.get("AEGIS_SENTRY_DSN")
    if not dsn:
        return

    try:
        import sentry_sdk  # noqa: PLC0415
        from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("ENV", "dev"),
            release=f"aegis@{os.environ.get('AEGIS_VERSION', 'dev')}",
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[FastApiIntegration(), StarletteIntegration()],
        )
        log.info("aegis sentry self-monitoring enabled")
    except ImportError:
        log.debug("sentry-sdk not installed (optional c3-e2e extra), skipping")


def register_providers(cfg: AegisSettings) -> None:
    """启动时注册 LLM provider 到 obase.ProviderRegistry."""
    try:
        import anthropic  # noqa: PLC0415

        def _anthropic_caller(
            *,
            messages: list,
            tools: list | None = None,
            max_tokens: int = 4096,
            stop_sequences: list[str] | None = None,
            model: str = "",
        ) -> dict:
            client = anthropic.Anthropic()
            kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            if stop_sequences:
                kwargs["stop_sequences"] = stop_sequences
            msg = client.messages.create(**kwargs)
            return {
                "content": [b.model_dump() for b in msg.content],
                "stop_reason": msg.stop_reason,
                "usage": {
                    "input_tokens": msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                },
            }

        ProviderRegistry.register("llm", "anthropic", _anthropic_caller, replace=True)
        log.info("registered llm provider: anthropic")
    except ImportError:
        log.debug("anthropic SDK not installed, skipping provider registration")

    # Ollama (本地兜底, AEGIS_DESIGN 决策 2)
    if cfg.ollama_base_url:
        import httpx  # noqa: PLC0415

        def _ollama_caller(
            *,
            messages: list,
            tools: list | None = None,
            max_tokens: int = 4096,
            stop_sequences: list[str] | None = None,
            model: str = "",
        ) -> dict:
            resp = httpx.post(
                f"{cfg.ollama_base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "content": [{"type": "text", "text": data.get("message", {}).get("content", "")}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": data.get("prompt_eval_count", 0),
                    "output_tokens": data.get("eval_count", 0),
                },
            }

        ProviderRegistry.register("llm", "ollama", _ollama_caller, replace=True)
        log.info("registered llm provider: ollama (base_url=%s)", cfg.ollama_base_url)
    else:
        log.debug("ollama provider not configured (set AEGIS_OLLAMA_BASE_URL to enable)")


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

        register_providers(cfg)

        from aegis.server.alert.platform_alerter import init_platform_alerter  # noqa: PLC0415
        from aegis.server.appstore.installer import init_app_installer  # noqa: PLC0415
        from aegis.server.brain.action_planner import init_planner_service  # noqa: PLC0415
        from aegis.server.brain.rca import init_rca_service  # noqa: PLC0415
        from aegis.server.brain.triage import init_triage_service  # noqa: PLC0415

        init_platform_alerter(cfg)
        init_rca_service(cfg)
        init_planner_service(cfg)
        init_triage_service(cfg)
        init_app_installer(cfg)

        yield

        log.info("aegis_shutting_down")
        await close_pool()

    app = FastAPI(
        title="Aegis",
        version="0.1.0",
        description="AI-powered self-hosted PaaS",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(alerts.router)
    app.include_router(alert_rules.router)
    app.include_router(alert_fired.router)
    app.include_router(release_gates_router.router)
    app.include_router(webhook_subscriptions_router.router)
    app.include_router(docker_router.router)
    app.include_router(apps_router.router)
    app.include_router(domains.router)
    app.include_router(projects_router.router)
    app.include_router(store_router.router)
    app.include_router(runbooks_router.router)
    app.include_router(auth_router.router)
    app.include_router(orgs_router.router)
    app.include_router(users_router.router)
    app.include_router(envelope_router.router)

    # C3-7: test error endpoint — dev/test only, never registered in prod
    if os.environ.get("ENV") != "prod":
        from aegis.server.api.routers import test_error  # noqa: PLC0415

        app.include_router(test_error.router)

    return app


init_sentry_if_enabled()
app = create_app()
