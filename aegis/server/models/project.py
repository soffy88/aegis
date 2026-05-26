"""Project model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class Project:
    id: UUID
    org_id: UUID
    slug: str
    name: str
    display_name: str
    environment: str
    docker_labels: dict[str, Any] | None
    config: dict[str, Any] | None
    archived_at: datetime | None
    created_at: datetime

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @classmethod
    def from_row(cls, row: dict) -> Project:
        import json

        def _parse_json(val: Any) -> dict[str, Any] | None:
            if val is None:
                return None
            return val if isinstance(val, dict) else json.loads(val)

        return cls(
            id=row["id"],
            org_id=row["org_id"],
            slug=row["slug"],
            name=row["name"],
            display_name=row["display_name"],
            environment=row.get("environment", "prod"),
            docker_labels=_parse_json(row.get("docker_labels")),
            config=_parse_json(row.get("config")),
            archived_at=row.get("archived_at"),
            created_at=row["created_at"],
        )
