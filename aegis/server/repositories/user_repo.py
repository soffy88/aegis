"""User repository."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from aegis.server.models import User


class UserRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self, *, email: str, password_hash: str, display_name: str | None = None
    ) -> User:
        row = await self.conn.fetchrow(
            """INSERT INTO users (email, password_hash, display_name)
               VALUES ($1, $2, $3) RETURNING *""",
            email,
            password_hash,
            display_name,
        )
        return User.from_row(row)

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self.conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return User.from_row(row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        row = await self.conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
        return User.from_row(row) if row else None

    async def update_last_login(self, user_id: UUID) -> None:
        await self.conn.execute("UPDATE users SET last_login_at = NOW() WHERE id = $1", user_id)

    async def bump_token_epoch(self, user_id: UUID) -> None:
        """Invalidate all of a user's outstanding access tokens immediately.

        Access tokens carry an `epoch` claim compared against this column on every
        request (see get_current_user); incrementing it makes existing tokens stale
        at once. Call on privilege-reducing changes: role change, org removal,
        deactivation, password change.
        """
        await self.conn.execute(
            "UPDATE users SET token_epoch = token_epoch + 1 WHERE id = $1", user_id
        )

    async def update_password(self, user_id: UUID, password_hash: str) -> None:
        # Bump epoch: a password change should not leave older sessions valid.
        await self.conn.execute(
            "UPDATE users SET password_hash = $1, token_epoch = token_epoch + 1 WHERE id = $2",
            password_hash,
            user_id,
        )

    async def set_active(self, user_id: UUID, *, is_active: bool) -> None:
        # Deactivation revokes existing access tokens at once (refresh is already
        # blocked by the is_active check); reactivation leaves the epoch untouched.
        if is_active:
            await self.conn.execute("UPDATE users SET is_active = TRUE WHERE id = $1", user_id)
        else:
            await self.conn.execute(
                "UPDATE users SET is_active = FALSE, token_epoch = token_epoch + 1 WHERE id = $1",
                user_id,
            )

    async def update_display_name(self, user_id: UUID, display_name: str) -> User | None:
        row = await self.conn.fetchrow(
            "UPDATE users SET display_name = $1 WHERE id = $2 RETURNING *",
            display_name,
            user_id,
        )
        return User.from_row(row) if row else None

    async def set_default_org(self, user_id: UUID, org_id: UUID) -> None:
        await self.conn.execute(
            "UPDATE users SET default_org_id = $1 WHERE id = $2", org_id, user_id
        )

    async def update_profile(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        default_org_id: UUID | None = None,
    ) -> User:
        """Partial profile update. None = leave unchanged."""
        row = await self.conn.fetchrow(
            "UPDATE users SET display_name = COALESCE($1, display_name),"
            " default_org_id = COALESCE($2, default_org_id) WHERE id = $3 RETURNING *",
            display_name,
            default_org_id,
            user_id,
        )
        if row is None:
            raise ValueError(f"user {user_id} not found")
        return User.from_row(row)
