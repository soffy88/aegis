"""Runbook indexer — 将 YAML runbooks 写入 LanceDB vector store."""

from __future__ import annotations

import logging

from oprim import VectorRecord
from oprim import vector_encode

from aegis.server.runtime.config import AegisSettings
from aegis.server.services.runbook import list_runbooks
from aegis.server.services.vector_store import get_vector_db

log = logging.getLogger(__name__)


def index_runbooks(cfg: AegisSettings) -> int:
    """将当前已加载的所有 runbook 写入 vector store.

    调用时机：服务启动时，load_runbooks() 之后。
    Returns: 写入条数
    """
    db = get_vector_db()
    if db is None:
        log.warning("runbook_indexer: vector_db not initialized, skipping")
        return 0

    runbooks = list_runbooks()
    if not runbooks:
        log.info("runbook_indexer: no runbooks loaded, nothing to index")
        return 0

    # 构造待编码文本：title + description + steps 摘要
    texts = []
    for rb in runbooks:
        steps_summary = "; ".join(s.name for s in rb.steps)
        text = f"{rb.name}: {rb.description}. Steps: {steps_summary}"
        texts.append(text)

    # 批量 encode
    vecs = vector_encode(
        texts=texts,
        provider=cfg.embedding_provider,
        normalize=True,
    )

    # 构造 VectorRecord
    records = []
    for i, rb in enumerate(runbooks):
        steps_detail = "\n".join(f"- {s.name} ({s.type}): {s.command}" for s in rb.steps)
        records.append(
            VectorRecord(
                id=rb.name,
                embedding=vecs[i].tolist(),
                metadata={
                    "title": rb.name,
                    "content": f"{rb.description}\n\nSteps:\n{steps_detail}",
                    "tags": [rb.trigger],
                    "requires_approval": rb.requires_approval,
                    "score": 1.0,  # placeholder，实际 score 由 search 时计算
                },
            )
        )

    db.upsert(records)
    log.info("runbook_indexer: indexed %d runbooks", len(records))
    return len(records)
