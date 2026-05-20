"""Shared test fixtures — uses in-memory mocking for DB-free tests."""
from __future__ import annotations

import uuid
from unittest import mock

import pytest


@pytest.fixture
def mock_db_conn() -> mock.AsyncMock:
    """Mock asyncpg.Connection. Tests configure fetchrow/fetch/execute return values."""
    return mock.AsyncMock()


@pytest.fixture
def test_org_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def test_project_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")
