"""Tests for C0c-1: LLM → obase.ProviderRegistry integration."""

from __future__ import annotations

from collections.abc import Generator
from unittest import mock

import pytest


@pytest.fixture
def mock_provider_registry() -> Generator[mock.MagicMock]:
    """Mock obase.ProviderRegistry for LLM tests."""
    fake_caller = mock.MagicMock(
        return_value={"content": [{"text": "hi"}], "stop_reason": "end_turn", "usage": {}}
    )
    with mock.patch("aegis.server.services.llm.ProviderRegistry") as m:
        m.get_caller.return_value = fake_caller
        yield m


@pytest.mark.asyncio
async def test_call_llm_uses_provider_registry(mock_provider_registry: mock.MagicMock) -> None:
    from aegis.server.services.llm import call_llm

    response = await call_llm(messages=[{"role": "user", "content": "hi"}])
    mock_provider_registry.get_caller.assert_called_once()
    args = mock_provider_registry.get_caller.call_args
    assert args[0][0] == "anthropic"  # default provider
    assert response["content"] == [{"text": "hi"}]


@pytest.mark.asyncio
async def test_call_llm_with_explicit_provider(mock_provider_registry: mock.MagicMock) -> None:
    from aegis.server.services.llm import call_llm

    await call_llm(messages=[{"role": "user", "content": "hi"}], provider="ollama", model="llama3")
    mock_provider_registry.get_caller.assert_called_once_with("ollama", "llama3")


@pytest.mark.asyncio
async def test_call_anthropic_deprecated_still_works(
    mock_provider_registry: mock.MagicMock,
) -> None:
    from aegis.server.services.llm import call_anthropic

    response = await call_anthropic(messages=[{"role": "user", "content": "hi"}])
    assert response
    mock_provider_registry.get_caller.assert_called_once_with("anthropic", "claude-sonnet-4-6")


def test_register_providers_at_startup() -> None:
    """startup 注册 anthropic provider."""
    with (
        mock.patch("aegis.server.app.ProviderRegistry") as m,
        mock.patch.dict("sys.modules", {"anthropic": mock.MagicMock()}),
    ):
        from aegis.server.app import register_providers
        from aegis.server.runtime.config import AegisSettings

        settings = AegisSettings()
        register_providers(settings)
        m.register.assert_called_once()
        call_args = m.register.call_args
        assert call_args[0][0] == "llm"
        assert call_args[0][1] == "anthropic"


def test_ollama_provider_registered_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama provider registered when AEGIS_OLLAMA_BASE_URL is set."""
    monkeypatch.setenv("AEGIS_OLLAMA_BASE_URL", "http://localhost:11434")
    with mock.patch("aegis.server.app.ProviderRegistry") as m:
        from aegis.server.app import register_providers
        from aegis.server.runtime.config import AegisSettings

        settings = AegisSettings()
        register_providers(settings)
        # Should register both anthropic and ollama
        calls = m.register.call_args_list
        categories_names = [(c[0][0], c[0][1]) for c in calls]
        assert ("llm", "ollama") in categories_names


def test_ollama_provider_not_registered_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama provider NOT registered when AEGIS_OLLAMA_BASE_URL is unset."""
    monkeypatch.delenv("AEGIS_OLLAMA_BASE_URL", raising=False)
    with mock.patch("aegis.server.app.ProviderRegistry") as m:
        from aegis.server.app import register_providers
        from aegis.server.runtime.config import AegisSettings

        settings = AegisSettings()
        register_providers(settings)
        calls = m.register.call_args_list
        categories_names = [(c[0][0], c[0][1]) for c in calls]
        assert ("llm", "ollama") not in categories_names
