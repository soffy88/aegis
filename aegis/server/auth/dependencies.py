"""FastAPI Depends: inject UserContext from Bearer token."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import asyncpg
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from obase.auth import jwt_verify_hs256

from aegis.server.api.deps import get_db_conn
from aegis.server.runtime.config import get_settings


@dataclass
class OrgInToken:
    org_id: UUID
    slug: str
    role: str  # one of the 5 RBAC roles


@dataclass
class UserContext:
    user_id: UUID
    email: str
    orgs: list[OrgInToken] = field(default_factory=list)

    def org_by_id(self, org_id: UUID) -> OrgInToken | None:
        return next((o for o in self.orgs if o.org_id == org_id), None)

    def org_by_slug(self, slug: str) -> OrgInToken | None:
        return next((o for o in self.orgs if o.slug == slug), None)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> UserContext:
    try:
        payload = jwt_verify_hs256(
            token=token,
            secret=get_settings().jwt_secret,
            check_exp=True,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="wrong token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Immediate-revocation check: the token's epoch must still match the DB, and the
    # user must be active. A role change / org removal / deactivation / password change
    # bumps users.token_epoch, so tokens minted before it are rejected here at once
    # (rather than lagging a full access-TTL). fetchval returns None for a missing or
    # inactive user. Pre-epoch tokens (no claim) fail the != check → one forced re-auth.
    user_id = UUID(payload["sub"])
    db_epoch = await conn.fetchval(
        "SELECT token_epoch FROM users WHERE id = $1 AND is_active", user_id
    )
    if db_epoch is None or payload.get("epoch") != db_epoch:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session no longer valid; please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserContext(
        user_id=UUID(payload["sub"]),
        email=payload["email"],
        orgs=[
            OrgInToken(
                org_id=UUID(o["org_id"]),
                slug=o["slug"],
                role=o["role"],
            )
            for o in payload.get("orgs", [])
        ],
    )
