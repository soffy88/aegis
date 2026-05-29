"""Auth endpoints — login / refresh / logout / me."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from obase.auth import argon2_verify, jwt_sign_hs256, jwt_verify_hs256
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.repositories import (
    MembershipRepository,
    OrgRepository,
    RevokedTokenRepository,
    UserRepository,
)
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Set httpOnly refresh cookie — Secure flag driven by config."""
    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=get_settings().jwt_refresh_secure,
        samesite="lax",
        max_age=30 * 86400,
        path="/api/v1/auth",
    )


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _issue_access_token(user_id: UUID, email: str, orgs: list[dict]) -> tuple[str, datetime]:
    """Sign an access token; returns (token, expires_at)."""
    settings = get_settings()
    ttl_seconds = settings.jwt_access_ttl_minutes * 60
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    token = jwt_sign_hs256(
        payload={"sub": str(user_id), "email": email, "orgs": orgs, "type": "access"},
        secret=settings.jwt_secret,
        expires_in_seconds=ttl_seconds,
    )
    return token, expires_at


def _issue_refresh_token(user_id: UUID) -> str:
    """Sign a refresh token with a fresh JTI; returns token."""
    settings = get_settings()
    return jwt_sign_hs256(
        payload={"sub": str(user_id), "jti": str(uuid4()), "type": "refresh"},
        secret=settings.jwt_secret,
        expires_in_seconds=settings.jwt_refresh_ttl_days * 86400,
    )


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

    if not user.password_hash or not argon2_verify(password=req.password, hash=user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    memberships = await membership_repo.list_by_user(user.id)
    orgs_for_token = []
    for m in memberships:
        org = await org_repo.get_by_id(m.org_id)
        if org:
            orgs_for_token.append({"org_id": str(org.id), "slug": org.slug, "role": m.role.value})

    access, access_exp = _issue_access_token(user.id, user.email, orgs_for_token)
    refresh = _issue_refresh_token(user.id)

    await user_repo.update_last_login(user.id)
    _set_refresh_cookie(response, refresh)

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
        payload = jwt_verify_hs256(
            token=refresh_token,
            secret=get_settings().jwt_secret,
            check_exp=True,
        )
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")

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

    access, access_exp = _issue_access_token(user.id, user.email, orgs_for_token)

    # Rotate: revoke the consumed JTI and issue a fresh refresh token.
    await revoked_repo.revoke(
        jti=payload["jti"],
        user_id=user.id,
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
    _set_refresh_cookie(response, _issue_refresh_token(user.id))

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
            payload = jwt_verify_hs256(
                token=refresh_token,
                secret=get_settings().jwt_secret,
                check_exp=True,
            )
            if payload.get("type") == "refresh":
                revoked_repo = RevokedTokenRepository(conn)
                await revoked_repo.revoke(
                    jti=payload["jti"],
                    user_id=UUID(payload["sub"]),
                    expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
                )
        except Exception:
            pass  # logout always succeeds — silently ignore invalid tokens

    response.delete_cookie("refresh_token", path="/api/v1/auth")


@router.get("/me")
async def me(user: UserContext = Depends(get_current_user)) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "orgs": [{"org_id": str(o.org_id), "slug": o.slug, "role": o.role} for o in user.orgs],
    }
