"""Shared test fixtures — uses in-memory mocking for DB-free tests."""

from __future__ import annotations

import os
import uuid
from unittest import mock

import pytest

# Set a valid JWT secret before any module that calls AegisSettings() is imported.
# The module-level `app = create_app()` in app.py runs during test collection;
# without this, any test that imports from aegis.server.app will fail.
os.environ.setdefault("AEGIS_JWT_SECRET", "test-secret-do-not-use-in-production-abc!")


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
