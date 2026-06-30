"""Secrets vault API (admin via MODIFY_ORG). Values are write-only — never returned."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/secrets", tags=["secrets"])


class SecretWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    value: str = Field(min_length=1, max_length=10000)


class SecretRotate(BaseModel):
    value: str = Field(min_length=1, max_length=10000)


@router.get("")
async def list_org_secrets(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> list[dict[str, Any]]:
    """List secret metadata (name/version/timestamps). Never returns values. admin+."""
    from aegis.server.services.secrets_vault import list_secrets

    return await list_secrets(conn, org_id=org_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def put_secret(
    org_id: uuid.UUID,
    body: SecretWrite,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> dict[str, Any]:
    """Create or replace a secret value (encrypted at rest). admin+."""
    from aegis.server.services.secrets_vault import store_secret

    return await store_secret(conn, org_id=org_id, name=body.name, value=body.value)


@router.post("/{name}/rotate")
async def rotate_org_secret(
    org_id: uuid.UUID,
    name: str,
    body: SecretRotate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> dict[str, Any]:
    """Rotate a secret to a new value (re-encrypt, version++). admin+."""
    from aegis.server.services.secrets_vault import rotate_secret

    meta = await rotate_secret(conn, org_id=org_id, name=name, new_value=body.value)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "secret not found")
    return meta


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    org_id: uuid.UUID,
    name: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> None:
    result = await conn.execute(
        "DELETE FROM org_secrets WHERE org_id = $1 AND name = $2", org_id, name
    )
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "secret not found")
