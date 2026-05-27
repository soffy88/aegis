"""Org model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class Org:
    id: UUID
    slug: str
    name: str
    plan: str
    status: str
    stripe_customer_id: str | None
    created_at: datetime

    @property
    def display_name(self) -> str:
        return self.name

    @classmethod
    def from_row(cls, row: dict) -> Org:
        return cls(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            plan=row["plan"],
            status=row.get("status", "active"),
            stripe_customer_id=row.get("stripe_customer_id"),
            created_at=row["created_at"],
        )
