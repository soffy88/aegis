"""Revoked refresh token repository — persists jti blocklist in Postgres."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg


class RevokedTokenRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def revoke(self, *, jti: str, user_id: UUID, expires_at: datetime) -> None:
        await self.conn.execute(
            """INSERT INTO revoked_tokens (jti, user_id, expires_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (jti) DO NOTHING""",
            jti,
            user_id,
            expires_at,
        )

    async def is_revoked(self, jti: str) -> bool:
        row = await self.conn.fetchrow("SELECT 1 FROM revoked_tokens WHERE jti = $1", jti)
        return row is not None

    async def cleanup_expired(self) -> int:
        """Delete entries where expires_at < NOW(). Returns count deleted."""
        result = await self.conn.execute("DELETE FROM revoked_tokens WHERE expires_at < NOW()")
        # asyncpg returns command tag string e.g. "DELETE 3"
        return int(result.split()[-1]) if result.startswith("DELETE") else 0
