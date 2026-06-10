"""Vector store service — LanceDB-backed runbook RAG.

Provides vector_encode_fn and vector_search_fn callables compatible
with oskill.retrieve_runbook's injection protocol.
"""

from __future__ import annotations

import logging
from typing import Any

from oprim.vector_db.lancedb import LanceDBVectorDB, open_vector_db
from oprim.vector_encode import vector_encode

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

_vector_db: LanceDBVectorDB | None = None
_embedding_provider: str = "default"
_vector_dim: int = 1024


def init_vector_store(cfg: AegisSettings) -> None:
    """初始化 LanceDB runbook vector store（进程启动时调用一次）."""
    global _vector_db, _embedding_provider, _vector_dim

    _embedding_provider = cfg.embedding_provider
    _vector_dim = cfg.runbook_vector_dim

    db_path = cfg.runbook_vector_db_path
    db_path.mkdir(parents=True, exist_ok=True)

    _vector_db = open_vector_db(
        path=db_path,
        table_name=cfg.runbook_vector_collection,
        dim=cfg.runbook_vector_dim,
    )
    log.info(
        "vector_store_initialized path=%s collection=%s dim=%d",
        db_path,
        cfg.runbook_vector_collection,
        cfg.runbook_vector_dim,
    )


def get_vector_db() -> LanceDBVectorDB | None:
    return _vector_db


def make_vector_encode_fn(provider: str = "default"):
    """返回 retrieve_runbook 兼容的 vector_encode_fn.

    retrieve_runbook 调用签名: vector_encode_fn(query: str) → list[float]
    oprim.vector_encode 签名:  vector_encode(texts=list[str], provider=str) → np.ndarray
    """

    def _encode(query: str) -> list[float]:
        vecs = vector_encode(texts=[query], provider=provider, normalize=True)
        return vecs[0].tolist()

    return _encode


def make_vector_search_fn(db: LanceDBVectorDB, collection: str):
    """返回 retrieve_runbook 兼容的 vector_search_fn.

    retrieve_runbook 调用签名:
        vector_search_fn(vector=list[float], collection=str, top_k=int) → list[dict]
    LanceDBVectorDB.search 签名:
        search(query_vec=list[float], top_k=int) → list[VectorRecord]

    映射规则（retrieve_runbook 期望的 dict 字段）:
        id       → VectorRecord.id
        score    → _distance → 1 / (1 + distance)（LanceDB 返回 L2 距离，转相似度）
        title    → VectorRecord.metadata["title"]
        content  → VectorRecord.metadata["content"]
        tags     → VectorRecord.metadata.get("tags", [])
    """

    def _search(
        vector: list[float],
        collection: str,  # noqa: ARG001 — ignored, db already scoped to collection
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        records = db.search(query_vec=vector, top_k=top_k)
        results = []
        for rec in records:
            meta = rec.metadata
            # LanceDB 默认返回 L2 距离，距离越小越相似
            # VectorRecord 里没有 score，用 metadata 里存的分数（upsert 时写入）
            # 若无，默认给 1.0（表示精确匹配，给 fallback 时用）
            score = float(meta.get("score", 1.0))
            results.append(
                {
                    "id": rec.id,
                    "runbook_id": rec.id,
                    "score": score,
                    "title": meta.get("title", ""),
                    "content": meta.get("content", ""),
                    "tags": meta.get("tags", []),
                }
            )
        return results

    return _search
