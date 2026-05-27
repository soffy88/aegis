"""Auth endpoints — login / refresh / logout / me."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.exceptions import TokenInvalidError
from aegis.server.auth.jwt_service import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from aegis.server.auth.password_service import verify_password
from aegis.server.repositories import (
    MembershipRepository,
    OrgRepository,
    RevokedTokenRepository,
    UserRepository,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    response: Response,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> TokenResponse:
    user_repo = UserRepository(conn)
    membership_repo = MembershipRepository(conn)
    org_repo = OrgRepository(conn)

    user = await user_repo.get_by_email(req.email)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if not user.password_hash or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    memberships = await membership_repo.list_by_user(user.id)
    orgs_for_token = []
    for m in memberships:
        org = await org_repo.get_by_id(m.org_id)
        if org:
            orgs_for_token.append({"org_id": str(org.id), "slug": org.slug, "role": m.role.value})

    access, access_exp = create_access_token(user_id=user.id, email=user.email, orgs=orgs_for_token)
    refresh, _refresh_exp, _jti = create_refresh_token(user_id=user.id)

    await user_repo.update_last_login(user.id)

    response.set_cookie(
        key="refresh_token",
        value=refresh,
        httponly=True,
        secure=False,  # M1 dev; set True in prod (HTTPS)
        samesite="lax",
        max_age=30 * 86400,
        path="/api/v1/auth",
    )

    return TokenResponse(
        access_token=access,
        expires_in=int((access_exp - datetime.now(UTC)).total_seconds()),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> TokenResponse:
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no refresh token")

    try:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
    except TokenInvalidError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    revoked_repo = RevokedTokenRepository(conn)
    if await revoked_repo.is_revoked(payload["jti"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")

    user_repo = UserRepository(conn)
    user = await user_repo.get_by_id(UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user inactive")

    membership_repo = MembershipRepository(conn)
    org_repo = OrgRepository(conn)
    memberships = await membership_repo.list_by_user(user.id)
    orgs_for_token = []
    for m in memberships:
        org = await org_repo.get_by_id(m.org_id)
        if org:
            orgs_for_token.append({"org_id": str(org.id), "slug": org.slug, "role": m.role.value})

    access, access_exp = create_access_token(user_id=user.id, email=user.email, orgs=orgs_for_token)

    return TokenResponse(
        access_token=access,
        expires_in=int((access_exp - datetime.now(UTC)).total_seconds()),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> None:
    if refresh_token:
        try:
            payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
            revoked_repo = RevokedTokenRepository(conn)
            await revoked_repo.revoke(
                jti=payload["jti"],
                user_id=UUID(payload["sub"]),
                expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
            )
        except TokenInvalidError:
            pass  # logout always succeeds — silently ignore invalid tokens

    response.delete_cookie("refresh_token", path="/api/v1/auth")


@router.get("/me")
async def me(user: UserContext = Depends(get_current_user)) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "orgs": [{"org_id": str(o.org_id), "slug": o.slug, "role": o.role} for o in user.orgs],
    }
