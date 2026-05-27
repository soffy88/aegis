"""User model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class User:
    id: UUID
    email: str
    password_hash: str | None
    default_org_id: UUID | None
    display_name: str | None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> User:
        return cls(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"],
            default_org_id=row["default_org_id"],
            display_name=row.get("display_name"),
            is_active=row.get("is_active", True),
            created_at=row["created_at"],
            last_login_at=row.get("last_login_at"),
        )

    def to_api_dict(self) -> dict:
        """API response (excludes password_hash)."""
        return {
            "id": str(self.id),
            "email": self.email,
            "display_name": self.display_name,
            "default_org_id": str(self.default_org_id) if self.default_org_id else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
