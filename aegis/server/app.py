"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from obase import ProviderRegistry

from aegis.server.exceptions import AegisError, QuotaExceededError

from aegis.server.api.routers import (
    alert_fired,
    alert_rules,
    alerts,
    domains,
    events,
    health,
)
from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import audit as audit_router
from aegis.server.api.routers import auth as auth_router
from aegis.server.api.routers import autoheal as autoheal_router
from aegis.server.api.routers import backups as backups_router
from aegis.server.api.routers import brain as brain_router
from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers import (
    edge as edge_router,
)
from aegis.server.api.routers import envelope as envelope_router
from aegis.server.api.routers import incidents as incidents_router
from aegis.server.api.routers import invite as invite_router
from aegis.server.api.routers import metrics as metrics_router
from aegis.server.api.routers import nodes as nodes_router
from aegis.server.api.routers import oncall as oncall_router
from aegis.server.api.routers import orgs as orgs_router
from aegis.server.api.routers import projects as projects_router
from aegis.server.api.routers import remediation as remediation_router
from aegis.server.api.routers import scrape_targets as scrape_targets_router
from aegis.server.api.routers import status_page as status_page_router
from aegis.server.api.routers import release_gates as release_gates_router
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.api.routers import store as store_router
from aegis.server.api.routers import users as users_router
from aegis.server.api.routers import webhook_subscriptions as webhook_subscriptions_router
from aegis.server.middleware.rate_limit import AuthRateLimitMiddleware
from aegis.server.middleware.request_id import RequestIDMiddleware
from aegis.server.middleware.security_headers import SecurityHeadersMiddleware
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
            system: str = "",
        ) -> dict:
            # 30s/2-retry overrides the SDK default 10-min timeout: an RCA ReAct
            # loop runs many sequential calls and must not wedge for minutes.
            client = anthropic.Anthropic(timeout=30.0, max_retries=2)
            kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
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
            system: str = "",
        ) -> dict:
            chat_messages = list(messages)
            if system:
                chat_messages = [{"role": "system", "content": system}, *chat_messages]
            resp = httpx.post(
                f"{cfg.ollama_base_url}/api/chat",
                json={"model": model, "messages": chat_messages, "stream": False},
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
    setup_logging(cfg.log_level, cfg.log_format)

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

        # From here on the pool is open and a cron task may start. Wrap in
        # try/finally so a partial-startup failure (migrations, vector index,
        # service init) still tears down the pool + any started cron instead of
        # leaking them.
        cron_task: asyncio.Task[None] | None = None
        try:
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
            from aegis.server.services.runbook import load_runbooks  # noqa: PLC0415
            from aegis.server.services.runbook_indexer import index_runbooks  # noqa: PLC0415
            from aegis.server.services.vector_store import init_vector_store  # noqa: PLC0415

            # 1. Load runbooks from YAML
            load_runbooks()

            # 2. Init LanceDB vector store
            init_vector_store(cfg)

            # 3. Index runbooks into vector store (RAG)
            index_runbooks(cfg)

            alerter = init_platform_alerter(cfg)
            init_rca_service(cfg)
            init_planner_service(cfg)
            init_triage_service(cfg)
            init_app_installer(cfg)

            from aegis.server.orchestration.cron import start_orchestration_crons  # noqa: PLC0415

            cron_task = start_orchestration_crons(alerter=alerter)

            yield
        finally:
            if cron_task is not None:
                cron_task.cancel()
                # Await cancellation so the loop isn't mid-iteration on the pool
                # when we close it.
                with suppress(asyncio.CancelledError):
                    await cron_task
            log.info("aegis_shutting_down")
            # Don't let a close error mask the original startup exception.
            with suppress(Exception):
                await close_pool()

    _is_prod = cfg.env == "prod"
    app = FastAPI(
        title="Aegis",
        version="0.1.0",
        description="AI-powered self-hosted PaaS",
        lifespan=lifespan,
        # Defense-in-depth: hide interactive docs/schema in prod. (Not reachable
        # through the /api-only edge proxy anyway, but closes the internal surface.)
        docs_url=None if _is_prod else "/docs",
        redoc_url=None if _is_prod else "/redoc",
        openapi_url=None if _is_prod else "/openapi.json",
    )

    @app.exception_handler(AegisError)
    async def _handle_aegis_error(request: Request, exc: AegisError) -> JSONResponse:
        """Map domain errors to a uniform {"error": {...}} envelope + status.

        Without this, a raised AegisError leaks as a bare Starlette 500 with no
        structured body. HTTPExceptions keep FastAPI's default handling.
        """
        code = (
            status.HTTP_402_PAYMENT_REQUIRED
            if isinstance(exc, QuotaExceededError)
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        log.warning(
            "aegis_error path=%s type=%s detail=%s",
            request.url.path,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=code,
            content={"error": {"type": type(exc).__name__, "detail": str(exc)}},
        )

    # Middleware order matters: Starlette wraps the LAST-added outermost. Add the
    # rate limiter first so CORS wraps it — otherwise a 429 is emitted outside
    # CORSMiddleware and the browser can't read it on the (CORS-governed) login form.
    app.add_middleware(
        AuthRateLimitMiddleware,
        max_requests=cfg.rate_limit_auth_requests,
        window_sec=cfg.rate_limit_auth_window_sec,
        redis_url=cfg.redis_url,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    # Hardening headers on every response (HSTS only in prod / over TLS).
    app.add_middleware(SecurityHeadersMiddleware, hsts=_is_prod)
    # Outermost: tag every request with a correlation id + emit an access line.
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health.router)
    app.include_router(metrics_router.router)
    app.include_router(scrape_targets_router.router)
    app.include_router(status_page_router.router)
    app.include_router(remediation_router.router)
    app.include_router(oncall_router.router)
    app.include_router(events.router)
    app.include_router(audit_router.router)
    app.include_router(alerts.router)
    app.include_router(alert_rules.router)
    app.include_router(alert_fired.router)
    app.include_router(release_gates_router.router)
    app.include_router(webhook_subscriptions_router.router)
    app.include_router(docker_router.router)
    app.include_router(autoheal_router.router)
    app.include_router(brain_router.router)
    app.include_router(nodes_router.router)
    app.include_router(apps_router.router)
    app.include_router(backups_router.router)
    app.include_router(domains.router)
    app.include_router(edge_router.router)
    app.include_router(projects_router.router)
    app.include_router(store_router.router)
    app.include_router(runbooks_router.router)
    app.include_router(auth_router.router)
    app.include_router(orgs_router.router)
    app.include_router(users_router.router)
    app.include_router(envelope_router.router)
    app.include_router(invite_router.router)
    app.include_router(incidents_router.router)

    # C3-7: test error endpoint — dev/test only, never registered in prod
    if os.environ.get("ENV") != "prod":
        from aegis.server.api.routers import test_error  # noqa: PLC0415

        app.include_router(test_error.router)

    return app


init_sentry_if_enabled()
app = create_app()
