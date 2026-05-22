"""Postgres async connection pool management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import asyncpg

from aegis.server.exceptions import AegisError

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(
    *,
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
) -> asyncpg.Pool:
    """Initialize the global asyncpg pool.

    Raises:
        AegisError: If pool init fails.
    """
    global _pool
    if _pool is not None:
        return _pool
    try:
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
        )
    except (asyncpg.PostgresError, OSError) as exc:
        raise AegisError(f"Failed to init Postgres pool: {exc}") from exc

    log.info("postgres pool initialized: min=%d max=%d", min_size, max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise AegisError("Postgres pool not initialized. Call init_pool() first.")
    return _pool


async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Async generator yielding a pooled connection."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn
