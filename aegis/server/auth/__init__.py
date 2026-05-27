"""Auth package — JWT, passwords, FastAPI dependencies."""

from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.exceptions import (
    AuthError,
    InvalidCredentialsError,
    TokenInvalidError,
    TokenRevokedError,
)
from aegis.server.auth.jwt_service import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from aegis.server.auth.password_service import hash_password, verify_password

__all__ = [
    "AuthError",
    "InvalidCredentialsError",
    "TokenInvalidError",
    "TokenRevokedError",
    "TokenType",
    "UserContext",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "verify_password",
]
