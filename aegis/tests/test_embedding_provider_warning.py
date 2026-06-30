"""Audit #13: surface the silent RAG embedding stub fallback at startup."""

from __future__ import annotations

import logging
from unittest import mock

from aegis.server.app import _warn_if_embeddings_stubbed
from aegis.server.runtime.config import AegisSettings


def test_warns_when_embedding_provider_unregistered(caplog):
    cfg = AegisSettings()
    with mock.patch("aegis.server.app.ProviderRegistry.has", return_value=False):
        with caplog.at_level(logging.WARNING):
            _warn_if_embeddings_stubbed(cfg)
    assert any("rag_embeddings_stubbed" in r.message for r in caplog.records)


def test_silent_when_embedding_provider_present(caplog):
    cfg = AegisSettings()
    with mock.patch("aegis.server.app.ProviderRegistry.has", return_value=True):
        with caplog.at_level(logging.WARNING):
            _warn_if_embeddings_stubbed(cfg)
    assert not any("rag_embeddings_stubbed" in r.message for r in caplog.records)
