"""Tests for the embedding provider abstraction (fastembed / ollama / lexical floor)."""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from aegis.server.runtime.config import AegisSettings
from aegis.server.services import embeddings


def _cfg(**over) -> AegisSettings:
    base = dict(embedding_provider="fastembed", embedding_model="BAAI/bge-small-en-v1.5")
    base.update(over)
    return AegisSettings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_warn():
    embeddings._warned = False
    yield


@pytest.mark.parametrize("provider", ["fts", "none", "", "lexical"])
def test_lexical_providers_return_none(provider: str) -> None:
    assert embeddings.get_embedder(_cfg(embedding_provider=provider)) is None


def test_unknown_provider_falls_back_to_lexical() -> None:
    assert embeddings.get_embedder(_cfg(embedding_provider="wat")) is None


def test_ollama_without_base_url_returns_none() -> None:
    assert embeddings.get_embedder(_cfg(embedding_provider="ollama", ollama_base_url="")) is None


def test_fastembed_missing_falls_back_to_lexical(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No fastembed installed → None (lexical), with a warning (not a crash)."""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # force ImportError on import
    with caplog.at_level("WARNING"):
        assert embeddings.get_embedder(_cfg(embedding_provider="fastembed")) is None
    assert any("fastembed" in r.message and "lexical" in r.message for r in caplog.records)


def test_fastembed_present_returns_working_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """With fastembed available, get_embedder returns a callable producing vectors."""
    fake_model = mock.Mock()
    fake_model.embed.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    fake_mod = types.ModuleType("fastembed")
    fake_mod.TextEmbedding = mock.Mock(return_value=fake_model)
    monkeypatch.setitem(sys.modules, "fastembed", fake_mod)

    embedder = embeddings.get_embedder(_cfg(embedding_provider="fastembed"))
    assert embedder is not None
    vecs = embedder(["hello", "world"])
    assert vecs == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    fake_mod.TextEmbedding.assert_called_once_with(model_name="BAAI/bge-small-en-v1.5")


def test_ollama_embedder_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = embeddings.get_embedder(
        _cfg(embedding_provider="ollama", ollama_base_url="http://ollama:11434")
    )
    assert embedder is not None
    resp = mock.Mock()
    resp.json.return_value = {"embedding": [1.0, 2.0]}
    resp.raise_for_status = mock.Mock()
    client = mock.MagicMock()
    client.post.return_value = resp
    client.__enter__.return_value = client
    with mock.patch.object(embeddings.httpx, "Client", return_value=client):
        out = embedder(["hi"])
    assert out == [[1.0, 2.0]]
    assert "api/embeddings" in client.post.call_args.args[0]
