"""Runbook RAG store — Postgres-backed (pgvector + pg_trgm), replaces LanceDB.

Persistence lives in the `runbook_vectors` table (durable, single-DB backup,
transactional). Retrieval runs against a small in-memory cache loaded from PG, so
the RCA agent (which calls knowledge_retrieval synchronously from a worker thread)
never needs an async DB round-trip. The corpus is tiny (dozens–hundreds of
runbooks), so in-process cosine / lexical scoring is instant.

Two tiers, chosen by whether an embedder is configured (see embeddings.get_embedder):
- semantic: cosine over pgvector-stored embeddings (embed the query at retrieve time)
- lexical (floor, no embedder): token-overlap over runbook text — works with zero
  deps / zero model / no GPU, so AI-RCA is functional everywhere.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

import asyncpg

from aegis.server.services.embeddings import Embedder
from aegis.server.services.runbook import Runbook

log = logging.getLogger(__name__)

# In-memory retrieval cache: list of {name,title,content,tags,requires_approval,embedding}
_INDEX: list[dict[str, Any]] = []


def _content_for(rb: Runbook) -> tuple[str, str]:
    """(title, content) — content is what gets embedded + lexically matched."""
    steps = "\n".join(f"- {s.name} ({s.type}): {s.command}" for s in rb.steps)
    content = f"{rb.name}: {rb.description}\n\nSteps:\n{steps}"
    return rb.name, content


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _vec_literal(vec: list[float]) -> str:
    """pgvector text input format: [a,b,c] — avoids needing an asyncpg codec."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _parse_vec(text: str | None) -> list[float] | None:
    if not text:
        return None
    return [float(x) for x in text.strip().lstrip("[").rstrip("]").split(",") if x]


async def sync_index(
    conn: asyncpg.Connection, runbooks: list[Runbook], embedder: Embedder | None
) -> int:
    """Upsert runbooks into PG (re-embedding only changed ones), prune removed, then
    reload the in-memory cache. Returns the number of runbooks indexed."""
    desired = []
    for rb in runbooks:
        title, content = _content_for(rb)
        desired.append((rb, title, content, _hash(content)))

    existing = {
        r["runbook_name"]: (r["content_hash"], r["has_emb"])
        for r in await conn.fetch(
            "SELECT runbook_name, content_hash, (embedding IS NOT NULL) AS has_emb "
            "FROM runbook_vectors"
        )
    }

    # Re-embed only runbooks whose content changed, or that lack an embedding while an
    # embedder is now available. Skipping unchanged ones means a normal reboot loads
    # no model at all (fast start).
    changed_idx = [
        i
        for i, (_rb, _t, _c, h) in enumerate(desired)
        if embedder is not None
        and (desired[i][0].name not in existing or existing[desired[i][0].name] != (h, True))
    ]
    new_vecs: dict[int, list[float]] = {}
    if changed_idx and embedder is not None:
        vecs = embedder([desired[i][2] for i in changed_idx])  # one model load for the batch
        new_vecs = dict(zip(changed_idx, vecs, strict=True))

    for i, (rb, title, content, h) in enumerate(desired):
        emb = new_vecs.get(i)
        await conn.execute(
            """
            INSERT INTO runbook_vectors
                (runbook_name, title, content, tags, requires_approval, content_hash, embedding)
            VALUES ($1,$2,$3,$4,$5,$6,$7::vector)
            ON CONFLICT (runbook_name) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                tags = EXCLUDED.tags,
                requires_approval = EXCLUDED.requires_approval,
                content_hash = EXCLUDED.content_hash,
                -- keep the existing embedding when this pass didn't recompute one
                embedding = COALESCE(EXCLUDED.embedding, runbook_vectors.embedding),
                updated_at = now()
            """,
            rb.name,
            title,
            content,
            [rb.trigger],
            rb.requires_approval,
            h,
            _vec_literal(emb) if emb is not None else None,
        )

    names = [rb.name for rb, *_ in desired]
    await conn.execute("DELETE FROM runbook_vectors WHERE runbook_name <> ALL($1::text[])", names)

    await reload_cache(conn)
    log.info(
        "runbook_store: indexed %d runbooks (%d re-embedded, embedder=%s)",
        len(desired),
        len(new_vecs),
        "on" if embedder else "off (lexical)",
    )
    return len(desired)


async def reload_cache(conn: asyncpg.Connection) -> None:
    """Load runbook records + embeddings from PG into the in-memory retrieval cache."""
    global _INDEX
    rows = await conn.fetch(
        "SELECT runbook_name, title, content, tags, requires_approval, embedding::text AS emb "
        "FROM runbook_vectors"
    )
    _INDEX = [
        {
            "name": r["runbook_name"],
            "title": r["title"],
            "content": r["content"],
            "tags": list(r["tags"] or []),
            "requires_approval": r["requires_approval"],
            "embedding": _parse_vec(r["emb"]),
        }
        for r in rows
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _lexical_score(query: str, content: str) -> float:
    """Token-overlap (Jaccard) + substring bonus — dependency-free lexical floor."""
    q = {t for t in query.lower().split() if len(t) > 2}
    c = {t for t in content.lower().split() if len(t) > 2}
    if not q:
        return 0.0
    jac = len(q & c) / len(q | c) if (q | c) else 0.0
    sub = 0.15 if query.lower().strip() and query.lower().strip() in content.lower() else 0.0
    return min(1.0, jac + sub)


def retrieve(
    query: str, *, top_k: int, min_score: float, embedder: Embedder | None
) -> list[dict[str, Any]]:
    """Rank cached runbooks against the query. Semantic (cosine) when an embedder is
    available, else lexical. Returns dicts: {runbook_id, title, content, score, tags}."""
    if not _INDEX:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    if embedder is not None:
        try:
            qv = embedder([query])[0]
        except Exception as exc:  # noqa: BLE001 — degrade to lexical on embed failure
            log.warning("runbook query embed failed (%s); lexical fallback", exc)
            qv = None
    else:
        qv = None

    threshold = min_score if qv is not None else min(min_score, 0.05)
    for rec in _INDEX:
        if qv is not None and rec["embedding"]:
            score = _cosine(qv, rec["embedding"])
        else:
            score = _lexical_score(query, rec["content"])
        if score >= threshold:
            scored.append((score, rec))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {
            "runbook_id": rec["name"],
            "title": rec["title"],
            "content": rec["content"],
            "tags": rec["tags"],
            "score": round(score, 4),
        }
        for score, rec in scored[:top_k]
    ]
