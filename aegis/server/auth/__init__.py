"""Auth package — JWT, passwords, FastAPI dependencies, RBAC."""

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
from aegis.server.auth.rbac import (
    PERMISSIONS_BY_ROLE,
    Permission,
    has_permission,
    require_min_role,
    require_permission,
)

__all__ = [
    "AuthError",
    "InvalidCredentialsError",
    "PERMISSIONS_BY_ROLE",
    "Permission",
    "TokenInvalidError",
    "TokenRevokedError",
    "TokenType",
    "UserContext",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_user",
    "has_permission",
    "hash_password",
    "require_min_role",
    "require_permission",
    "verify_password",
]
