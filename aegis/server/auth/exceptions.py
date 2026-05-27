"""Auth exceptions."""


class AuthError(Exception):
    """Base for all auth errors."""


class TokenInvalidError(AuthError):
    """JWT decode failed / wrong type / expired."""


class TokenRevokedError(AuthError):
    """Refresh token jti in revoked list."""


class InvalidCredentialsError(AuthError):
    """Email not found / password mismatch / user inactive."""
