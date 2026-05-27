"""Org repository."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from aegis.server.models import Org


class OrgRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(self, *, slug: str, name: str, plan: str = "free") -> Org:
        row = await self.conn.fetchrow(
            "INSERT INTO orgs (slug, name, plan) VALUES ($1, $2, $3) RETURNING *",
            slug,
            name,
            plan,
        )
        return Org.from_row(row)

    async def get_by_id(self, org_id: UUID) -> Org | None:
        row = await self.conn.fetchrow("SELECT * FROM orgs WHERE id = $1", org_id)
        return Org.from_row(row) if row else None

    async def get_by_slug(self, slug: str) -> Org | None:
        row = await self.conn.fetchrow("SELECT * FROM orgs WHERE slug = $1", slug)
        return Org.from_row(row) if row else None

    async def list_by_user(self, user_id: UUID) -> list[Org]:
        rows = await self.conn.fetch(
            """SELECT o.* FROM orgs o
               INNER JOIN org_memberships m ON m.org_id = o.id
               WHERE m.user_id = $1 ORDER BY o.created_at""",
            user_id,
        )
        return [Org.from_row(r) for r in rows]

    async def update(
        self, org_id: UUID, *, name: str | None = None, plan: str | None = None
    ) -> Org | None:
        org = await self.get_by_id(org_id)
        if not org:
            return None
        new_name = name if name is not None else org.name
        new_plan = plan if plan is not None else org.plan
        row = await self.conn.fetchrow(
            "UPDATE orgs SET name = $1, plan = $2 WHERE id = $3 RETURNING *",
            new_name,
            new_plan,
            org_id,
        )
        return Org.from_row(row) if row else None

    async def update_name(self, org_id: UUID, name: str) -> Org | None:
        """Update org display name. Returns updated org or None if not found."""
        return await self.update(org_id, name=name)

    async def delete(self, org_id: UUID) -> bool:
        result = await self.conn.execute("DELETE FROM orgs WHERE id = $1", org_id)
        return result == "DELETE 1"
