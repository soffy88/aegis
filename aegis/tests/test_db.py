"""Tests for persistence.db module."""

from __future__ import annotations

from collections.abc import Generator
from unittest import mock

import pytest

from aegis.server.exceptions import AegisError
from aegis.server.persistence import db as db_module
from aegis.server.persistence.db import close_pool, get_pool, init_pool


@pytest.fixture(autouse=True)
def reset_pool() -> Generator[None, None, None]:
    """Reset the module-level _pool between tests."""
    db_module._pool = None
    yield
    db_module._pool = None


class TestInitPool:
    @pytest.mark.asyncio
    async def test_init_pool_success(self) -> None:
        fake_pool = mock.MagicMock()
        with mock.patch(
            "asyncpg.create_pool",
            new_callable=mock.AsyncMock,
            return_value=fake_pool,
        ) as m:
            result = await init_pool(dsn="postgresql://x:x@localhost/x")
        assert result is fake_pool
        m.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_pool_already_initialized(self) -> None:
        fake_pool = mock.AsyncMock()
        db_module._pool = fake_pool
        result = await init_pool(dsn="postgresql://x:x@localhost/x")
        assert result is fake_pool

    @pytest.mark.asyncio
    async def test_init_pool_oserror_raises_aegis_error(self) -> None:
        with (
            mock.patch("asyncpg.create_pool", side_effect=OSError("refused")),
            pytest.raises(AegisError, match="Failed to init"),
        ):
            await init_pool(dsn="postgresql://x:x@localhost/x")


class TestClosePool:
    @pytest.mark.asyncio
    async def test_close_pool_noop_when_none(self) -> None:
        await close_pool()  # should not raise

    @pytest.mark.asyncio
    async def test_close_pool_calls_close(self) -> None:
        fake_pool = mock.AsyncMock()
        db_module._pool = fake_pool
        await close_pool()
        fake_pool.close.assert_called_once()
        assert db_module._pool is None


class TestGetPool:
    def test_get_pool_raises_when_not_initialized(self) -> None:
        with pytest.raises(AegisError, match="not initialized"):
            get_pool()

    def test_get_pool_returns_pool(self) -> None:
        fake_pool = mock.MagicMock()
        db_module._pool = fake_pool
        assert get_pool() is fake_pool


class TestAcquire:
    @pytest.mark.asyncio
    async def test_acquire_yields_connection(self) -> None:
        fake_conn = mock.AsyncMock()
        fake_pool = mock.MagicMock()
        fake_pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=fake_conn)
        fake_pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=None)
        db_module._pool = fake_pool

        from aegis.server.persistence.db import acquire

        async for conn in acquire():
            assert conn is fake_conn
