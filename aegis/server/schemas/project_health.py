"""Project health schema — standard health protocol for Aegis-managed projects."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class ProjectHealth(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str | None = None
    checks: dict[str, Any] = {}
    timestamp: datetime
