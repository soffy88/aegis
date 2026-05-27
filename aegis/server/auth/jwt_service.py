"""JWT issue + verify (HS256).

Access token:  1 hr,  carries user_id + email + orgs[{org_id, slug, role}].
Refresh token: 30 days, carries jti (server-side revocable via revoked_tokens table).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from jose import JWTError, jwt

from aegis.server.runtime.config import get_settings


class TokenType:
    ACCESS = "access"
    REFRESH = "refresh"


def create_access_token(*, user_id: UUID, email: str, orgs: list[dict]) -> tuple[str, datetime]:
    """Return (token, expires_at). orgs = [{org_id, slug, role}, ...]."""
    settings = get_settings()
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.jwt_access_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "email": email,
        "orgs": orgs,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": TokenType.ACCESS,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, exp


def create_refresh_token(*, user_id: UUID) -> tuple[str, datetime, str]:
    """Return (token, expires_at, jti)."""
    settings = get_settings()
    now = datetime.now(UTC)
    exp = now + timedelta(days=settings.jwt_refresh_ttl_days)
    jti = str(uuid4())
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": TokenType.REFRESH,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, exp, jti


def decode_token(token: str, *, expected_type: str) -> dict[str, Any]:
    """Decode and validate token. Raises TokenInvalidError on any failure."""
    from aegis.server.auth.exceptions import TokenInvalidError  # avoid circular import

    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        raise TokenInvalidError(f"jwt decode failed: {e}") from e

    if payload.get("type") != expected_type:
        raise TokenInvalidError(
            f"wrong token type: expected {expected_type!r}, got {payload.get('type')!r}"
        )

    return payload
