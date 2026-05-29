"""Auth package — JWT, passwords, FastAPI dependencies, RBAC."""

from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.exceptions import (
    AuthError,
    InvalidCredentialsError,
    TokenInvalidError,
    TokenRevokedError,
)
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
    "UserContext",
    "get_current_user",
    "has_permission",
    "require_min_role",
    "require_permission",
]
