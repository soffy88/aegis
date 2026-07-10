"""Runbook indexer — 将 YAML runbooks 写入 Postgres 的 runbook_vectors (pgvector/pg_trgm)。

调用时机：服务启动时，load_runbooks() 之后。存储与检索见 services/runbook_store.py。
"""

from __future__ import annotations

import logging

import asyncpg

from aegis.server.runtime.config import AegisSettings
from aegis.server.services import runbook_store
from aegis.server.services.embeddings import get_embedder
from aegis.server.services.runbook import list_runbooks

log = logging.getLogger(__name__)


async def index_runbooks(cfg: AegisSettings, conn: asyncpg.Connection) -> int:
    """Index all currently-loaded runbooks into PG. Returns count indexed."""
    runbooks = list_runbooks()
    if not runbooks:
        log.info("runbook_indexer: no runbooks loaded")
    # sync_index also prunes rows for deleted runbooks + refreshes the retrieval cache.
    return await runbook_store.sync_index(conn, runbooks, get_embedder(cfg))
