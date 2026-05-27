"""Membership repository."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from aegis.server.models import OrgMembership, Role, User


class MembershipRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def add(self, *, user_id: UUID, org_id: UUID, role: Role) -> OrgMembership:
        row = await self.conn.fetchrow(
            """INSERT INTO org_memberships (org_id, user_id, role)
               VALUES ($1, $2, $3) RETURNING *""",
            org_id,
            user_id,
            role.value,
        )
        return OrgMembership.from_row(row)

    async def remove(self, *, user_id: UUID, org_id: UUID) -> bool:
        result = await self.conn.execute(
            "DELETE FROM org_memberships WHERE org_id = $1 AND user_id = $2",
            org_id,
            user_id,
        )
        return result == "DELETE 1"

    async def get(self, *, user_id: UUID, org_id: UUID) -> OrgMembership | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM org_memberships WHERE org_id = $1 AND user_id = $2",
            org_id,
            user_id,
        )
        return OrgMembership.from_row(row) if row else None

    async def list_by_user(self, user_id: UUID) -> list[OrgMembership]:
        rows = await self.conn.fetch(
            "SELECT * FROM org_memberships WHERE user_id = $1 ORDER BY joined_at",
            user_id,
        )
        return [OrgMembership.from_row(r) for r in rows]

    async def list_by_org(self, org_id: UUID) -> list[tuple[OrgMembership, User]]:
        rows = await self.conn.fetch(
            """SELECT m.*, u.id as u_id, u.email, u.password_hash, u.default_org_id,
                      u.display_name, u.is_active, u.created_at as u_created_at, u.last_login_at
               FROM org_memberships m
               INNER JOIN users u ON u.id = m.user_id
               WHERE m.org_id = $1 ORDER BY m.joined_at""",
            org_id,
        )
        result = []
        for r in rows:
            membership = OrgMembership.from_row(r)
            user = User(
                id=r["u_id"],
                email=r["email"],
                password_hash=r["password_hash"],
                default_org_id=r["default_org_id"],
                display_name=r["display_name"],
                is_active=r["is_active"],
                created_at=r["u_created_at"],
                last_login_at=r["last_login_at"],
            )
            result.append((membership, user))
        return result

    async def update_role(
        self, *, user_id: UUID, org_id: UUID, new_role: Role
    ) -> OrgMembership | None:
        row = await self.conn.fetchrow(
            """UPDATE org_memberships SET role = $1
               WHERE org_id = $2 AND user_id = $3 RETURNING *""",
            new_role.value,
            org_id,
            user_id,
        )
        return OrgMembership.from_row(row) if row else None

    async def list_by_org_with_users(self, org_id: UUID) -> list[tuple[OrgMembership, User]]:
        """Alias for list_by_org; returns memberships with full user details."""
        return await self.list_by_org(org_id)

    async def count_owners_in_org(self, org_id: UUID) -> int:
        return await self.conn.fetchval(
            "SELECT COUNT(*) FROM org_memberships WHERE org_id = $1 AND role = 'owner'",
            org_id,
        )
