"""Embedding provider abstraction for runbook RAG.

Runbook embedding is a *low-frequency* operation (index on runbook change + embed
the query on each incident RCA), so nothing here is a resident daemon or needs a GPU.
Three providers, chosen by `AEGIS_EMBEDDING_PROVIDER`:

- ``fastembed`` — in-process CPU (ONNX) via the optional ``fastembed`` extra. Small
  model (bge-small, ~130MB). If the extra isn't installed, we WARN and fall back to
  lexical retrieval (pg_trgm) so AI-RCA still works — semantic is an enhancement.
- ``ollama``   — offload to Ollama's /api/embeddings (CPU or GPU on the Ollama host;
  set Ollama's keep_alive=0 for non-resident). No local model / dependency.
- ``fts`` / ``none`` / unset → None: pure pg_trgm lexical retrieval (zero deps, zero
  memory, works on any host). The always-available floor.

get_embedder returns ``Callable[[list[str]], list[list[float]]]`` or ``None`` (→ lexical).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import httpx

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

Embedder = Callable[[Sequence[str]], list[list[float]]]

_LEXICAL_PROVIDERS = {"", "fts", "none", "lexical"}
_warned = False


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        log.warning(msg)
        _warned = True


def _fastembed_embedder(model_name: str) -> Embedder | None:
    """CPU ONNX embedder. Model loaded per call and released (non-resident) — fine
    for the low call rate; keeps idle RAM at ~0."""
    try:
        from fastembed import TextEmbedding  # noqa: PLC0415
    except ImportError:
        _warn_once(
            "embedding provider 'fastembed' not installed (pip install 'aegis[embed]'); "
            "runbook retrieval falls back to lexical pg_trgm (semantic disabled)"
        )
        return None

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        model = TextEmbedding(model_name=model_name)  # loads ONNX (CPU)
        out = [list(map(float, v)) for v in model.embed(list(texts))]
        return out  # `model` goes out of scope → released (non-resident)

    return _embed


def _ollama_embedder(base_url: str, model_name: str) -> Embedder:
    """Offload embedding to Ollama (CPU or GPU on its host). Non-resident if the
    Ollama server runs with keep_alive=0."""
    url = f"{base_url.rstrip('/')}/api/embeddings"

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        with httpx.Client(timeout=30) as client:
            for t in texts:
                r = client.post(url, json={"model": model_name, "prompt": t})
                r.raise_for_status()
                vecs.append([float(x) for x in r.json()["embedding"]])
        return vecs

    return _embed


def get_embedder(cfg: AegisSettings) -> Embedder | None:
    """Resolve the embedder for the configured provider, or None for lexical-only."""
    provider = (cfg.embedding_provider or "").strip().lower()
    if provider in _LEXICAL_PROVIDERS:
        return None
    if provider in ("fastembed", "default"):
        return _fastembed_embedder(cfg.embedding_model)
    if provider == "ollama":
        base = cfg.ollama_base_url or ""
        if not base:
            _warn_once(
                "embedding provider 'ollama' but AEGIS_OLLAMA_BASE_URL unset; lexical fallback"
            )
            return None
        return _ollama_embedder(base, cfg.embedding_model)
    _warn_once(f"unknown embedding provider {provider!r}; lexical (pg_trgm) fallback")
    return None
