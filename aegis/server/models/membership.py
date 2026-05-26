"""OrgMembership model + Role enum."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    MEMBER = "member"
    VIEWER = "viewer"


ROLE_HIERARCHY: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 10,
    Role.MEMBER: 20,
    Role.ADMIN: 30,
    Role.OWNER: 40,
}


@dataclass
class OrgMembership:
    org_id: UUID
    user_id: UUID
    role: Role
    joined_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> OrgMembership:
        return cls(
            org_id=row["org_id"],
            user_id=row["user_id"],
            role=Role(row["role"]),
            joined_at=row["joined_at"],
        )
