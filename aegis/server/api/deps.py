"""FastAPI dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
from fastapi import Header, HTTPException, status

from aegis.server.persistence import acquire


async def get_db_conn() -> AsyncIterator[asyncpg.Connection]:
    """Yield a pooled DB connection."""
    async for conn in acquire():
        yield conn


def require_org(
    x_org_id: str | None = Header(default=None),
) -> uuid.UUID:
    """Extract org_id from header. In self-hosted mode, a default org is used."""
    if not x_org_id:
        # Self-hosted default
        return uuid.UUID("00000000-0000-0000-0000-000000000001")
    try:
        return uuid.UUID(x_org_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Org-Id header",
        ) from exc


def require_project(
    x_project_id: str | None = Header(default=None),
) -> uuid.UUID:
    if not x_project_id:
        return uuid.UUID("00000000-0000-0000-0000-000000000002")
    try:
        return uuid.UUID(x_project_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Project-Id header",
        ) from exc
