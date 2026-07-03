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

    async def update_password(self, user_id: UUID, password_hash: str) -> None:
        await self.conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2", password_hash, user_id
        )

    async def set_active(self, user_id: UUID, *, is_active: bool) -> None:
        await self.conn.execute("UPDATE users SET is_active = $1 WHERE id = $2", is_active, user_id)

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
        current = await self.get_by_id(user_id)
        if not current:
            raise ValueError(f"user {user_id} not found")
        new_display_name = display_name if display_name is not None else current.display_name
        new_default_org = default_org_id if default_org_id is not None else current.default_org_id
        row = await self.conn.fetchrow(
            "UPDATE users SET display_name = $1, default_org_id = $2 WHERE id = $3 RETURNING *",
            new_display_name,
            new_default_org,
            user_id,
        )
        return User.from_row(row)
