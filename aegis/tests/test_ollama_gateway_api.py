"""API tests for the Ollama gateway router (§5.2 single-GPU multi-project sharing)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import ollama_gateway as router_mod
from aegis.server.runtime.config import AegisSettings, get_settings


def _cfg(**over) -> AegisSettings:
    base = dict(
        ollama_base_url="http://host.docker.internal:11434",
        ollama_gateway_token="",
        ollama_gateway_max_concurrency=1,
        ollama_gateway_queue_timeout_sec=5.0,
    )
    base.update(over)
    return AegisSettings(**base)  # type: ignore[call-arg]


def _client(cfg: AegisSettings) -> TestClient:
    fa = FastAPI()
    fa.include_router(router_mod.router)
    fa.dependency_overrides[get_settings] = lambda: cfg

    async def _conn() -> AsyncIterator[AsyncMock]:
        yield AsyncMock()

    fa.dependency_overrides[get_db_conn] = _conn
    return TestClient(fa, raise_server_exceptions=False)


def test_no_token_configured_allows_any_request():
    with patch(
        "aegis.server.services.ollama_gateway.list_models",
        AsyncMock(return_value={"models": []}),
    ):
        r = _client(_cfg()).get("/api/v1/llm/ollama/tags")
    assert r.status_code == 200


def test_token_configured_rejects_missing_header():
    r = _client(_cfg(ollama_gateway_token="secret")).get("/api/v1/llm/ollama/tags")
    assert r.status_code == 401


def test_token_configured_rejects_wrong_token():
    r = _client(_cfg(ollama_gateway_token="secret")).get(
        "/api/v1/llm/ollama/tags", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


def test_token_configured_accepts_correct_token():
    with patch(
        "aegis.server.services.ollama_gateway.list_models",
        AsyncMock(return_value={"models": []}),
    ):
        r = _client(_cfg(ollama_gateway_token="secret")).get(
            "/api/v1/llm/ollama/tags", headers={"Authorization": "Bearer secret"}
        )
    assert r.status_code == 200


def test_base_url_unconfigured_returns_503():
    r = _client(_cfg(ollama_base_url=None)).get("/api/v1/llm/ollama/tags")
    assert r.status_code == 503


def test_generate_success_passthrough():
    with patch(
        "aegis.server.services.ollama_gateway.generate",
        AsyncMock(return_value={"response": "hi"}),
    ) as gen:
        r = _client(_cfg()).post(
            "/api/v1/llm/ollama/generate", json={"model": "qwen2.5:7b", "prompt": "hi"}
        )
    assert r.status_code == 200
    assert r.json() == {"response": "hi"}
    gen.assert_awaited_once()
    kw = gen.await_args.kwargs
    assert kw["max_concurrency"] == 1 and kw["queue_timeout_sec"] == 5.0


def test_generate_busy_returns_503():
    from aegis.server.services.ollama_gateway import GatewayBusyError

    with patch(
        "aegis.server.services.ollama_gateway.generate",
        AsyncMock(side_effect=GatewayBusyError("busy")),
    ):
        r = _client(_cfg()).post("/api/v1/llm/ollama/generate", json={"model": "m", "prompt": "p"})
    assert r.status_code == 503


def test_generate_upstream_error_returns_502():
    from aegis.server.services.ollama_gateway import GatewayUpstreamError

    with patch(
        "aegis.server.services.ollama_gateway.generate",
        AsyncMock(side_effect=GatewayUpstreamError("boom", status_code=500)),
    ):
        r = _client(_cfg()).post("/api/v1/llm/ollama/generate", json={"model": "m", "prompt": "p"})
    assert r.status_code == 502


def test_chat_success_passthrough():
    with patch(
        "aegis.server.services.ollama_gateway.chat",
        AsyncMock(return_value={"message": {"role": "assistant", "content": "hi"}}),
    ):
        r = _client(_cfg()).post(
            "/api/v1/llm/ollama/chat",
            json={"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert r.json()["message"]["content"] == "hi"
