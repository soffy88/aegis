"""Tests for FastAPI dependencies."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from aegis.server.api.deps import require_org, require_project


class TestRequireOrg:
    def test_no_header_returns_default(self) -> None:
        result = require_org(x_org_id=None)
        assert result == uuid.UUID("00000000-0000-0000-0000-000000000001")

    def test_valid_uuid_header(self) -> None:
        uid = "11111111-1111-1111-1111-111111111111"
        result = require_org(x_org_id=uid)
        assert result == uuid.UUID(uid)

    def test_invalid_uuid_raises_400(self) -> None:
        with pytest.raises(HTTPException) as exc:
            require_org(x_org_id="not-a-uuid")
        assert exc.value.status_code == 400


class TestRequireProject:
    def test_no_header_returns_default(self) -> None:
        result = require_project(x_project_id=None)
        assert result == uuid.UUID("00000000-0000-0000-0000-000000000002")

    def test_valid_uuid_header(self) -> None:
        uid = "22222222-2222-2222-2222-222222222222"
        result = require_project(x_project_id=uid)
        assert result == uuid.UUID(uid)

    def test_invalid_uuid_raises_400(self) -> None:
        with pytest.raises(HTTPException) as exc:
            require_project(x_project_id="bad")
        assert exc.value.status_code == 400
