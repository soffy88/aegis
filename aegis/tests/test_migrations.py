"""Tests for migrations runner."""
from __future__ import annotations

from unittest import mock

import pytest

from aegis.server.persistence.migrations import MIGRATIONS, apply_migrations


def _make_conn(applied_versions: list[str]) -> mock.AsyncMock:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"version": v} for v in applied_versions]
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=None)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    # conn.transaction must be a regular (non-async) mock so conn.transaction()
    # returns ctx directly rather than a coroutine
    conn.transaction = mock.MagicMock(return_value=ctx)
    return conn


class TestApplyMigrations:
    @pytest.mark.asyncio
    async def test_applies_all_when_none_applied(self) -> None:
        conn = _make_conn([])
        count = await apply_migrations(conn)
        assert count == len(MIGRATIONS)

    @pytest.mark.asyncio
    async def test_skips_already_applied(self) -> None:
        conn = _make_conn([v for v, _ in MIGRATIONS])
        count = await apply_migrations(conn)
        assert count == 0

    @pytest.mark.asyncio
    async def test_applies_only_missing(self) -> None:
        conn = _make_conn([MIGRATIONS[0][0]])
        count = await apply_migrations(conn)
        assert count == len(MIGRATIONS) - 1


class TestSeedDefaults:
    """Tests for 004_seed_self_hosted_defaults migration."""

    def test_004_in_migrations_list(self) -> None:
        from aegis.server.persistence.migrations import MIGRATIONS
        versions = [v for v, _ in MIGRATIONS]
        assert "004_seed_self_hosted_defaults" in versions

    def test_004_sql_contains_default_org(self) -> None:
        from aegis.server.persistence.migrations import MIGRATIONS
        for version, sql in MIGRATIONS:
            if version == "004_seed_self_hosted_defaults":
                assert "00000000-0000-0000-0000-000000000001" in sql
                assert "enterprise" in sql
                return
        raise AssertionError("004 migration not found")

    def test_004_sql_contains_default_project(self) -> None:
        from aegis.server.persistence.migrations import MIGRATIONS
        for version, sql in MIGRATIONS:
            if version == "004_seed_self_hosted_defaults":
                assert "00000000-0000-0000-0000-000000000002" in sql
                assert "prod" in sql
                return
        raise AssertionError("004 migration not found")
