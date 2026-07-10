"""Tests for SLO error-budget computation — focus: tenant isolation of the span query."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.api.routers.slo import _compute


@pytest.mark.asyncio
async def test_compute_scopes_span_query_by_org_id() -> None:
    """_compute must filter aegis_spans by the SLO's org_id, not just service name —
    otherwise an SLO named after a service common to another tenant leaks that
    tenant's span volume / SLI (cross-tenant data disclosure)."""
    org = uuid.uuid4()
    row = {
        "id": uuid.uuid4(),
        "org_id": org,
        "name": "api-availability",
        "service": "api",
        "objective": 99.5,
        "window_days": 30,
    }
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"total": 100, "errors": 1}

    out = await _compute(conn, row)

    sql, *args = conn.fetchrow.call_args.args
    assert "org_id = $3" in sql  # org filter present
    assert org in args  # the SLO's org_id is bound
    assert out["current_sli"] == 99.0
