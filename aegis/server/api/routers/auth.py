"""Auth endpoints — login / refresh / logout / me."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from obase.auth import argon2_verify, jwt_sign_hs256, jwt_verify_hs256
from pydantic import BaseModel, Field

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
        path="/",
    )


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=12)
    org_name: str = Field(..., min_length=1)
    org_slug: str = Field(..., min_length=1, pattern=r"^[a-z0-9-]+$")


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


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    response: Response,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict:
    """注册新用户，自动创建 org，自动登录返回 token."""
    from obase.auth import argon2_hash  # noqa: PLC0415

    # 检查邮箱是否已存在（事务外做，避免持锁）
    existing = await conn.fetchval("SELECT id FROM users WHERE email = $1", req.email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    slug_exists = await conn.fetchval("SELECT id FROM orgs WHERE slug = $1", req.org_slug)
    if slug_exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Org slug already taken")

    # 提前计算密码哈希（CPU 密集，避免在事务内持锁）
    pw_hash = argon2_hash(password=req.password)

    org_id = uuid4()
    user_id = uuid4()

    # 所有写操作放在一个事务里，保证原子性
    async with conn.transaction():
        await conn.execute(
            """INSERT INTO orgs (id, slug, name, plan, created_at)
               VALUES ($1, $2, $3, 'enterprise', now())""",
            org_id,
            req.org_slug,
            req.org_name,
        )
        await conn.execute(
            """INSERT INTO users (id, email, password_hash, is_active, created_at)
               VALUES ($1, $2, $3, true, now())""",
            user_id,
            req.email,
            pw_hash,
        )
        await conn.execute(
            """INSERT INTO org_memberships (user_id, org_id, role, joined_at)
               VALUES ($1, $2, 'owner', now())""",
            user_id,
            org_id,
        )

    # 自动登录（在事务外，避免 token 签发失败导致回滚）
    orgs_for_token = [{"org_id": str(org_id), "slug": req.org_slug, "role": "owner"}]
    access, access_exp = _issue_access_token(user_id, req.email, orgs_for_token)
    refresh = _issue_refresh_token(user_id)
    _set_refresh_cookie(response, refresh)

    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": int((access_exp - datetime.now(UTC)).total_seconds()),
        "user_id": str(user_id),
        "org_id": str(org_id),
        "org_slug": req.org_slug,
    }


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

    response.delete_cookie("refresh_token", path="/")


@router.get("/me")
async def me(user: UserContext = Depends(get_current_user)) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "orgs": [{"org_id": str(o.org_id), "slug": o.slug, "role": o.role} for o in user.orgs],
    }
