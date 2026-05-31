"""Pydantic schemas for release_gates."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

DEFAULT_EXPIRY_HOURS = 24


class ReleaseGateCreate(BaseModel):
    action_kind: str = Field(min_length=1, max_length=100)
    action_payload: dict[str, Any] = Field(default_factory=dict)
    autoheal_event_id: UUID | None = None
    expires_in_hours: int = Field(default=DEFAULT_EXPIRY_HOURS, ge=1, le=168)


class ReleaseGateDecide(BaseModel):
    decision: Literal["approved", "rejected"]
    decision_reason: str = Field(min_length=1, max_length=1000)


class ReleaseGateResponse(BaseModel):
    gate_id: UUID
    org_id: UUID
    project_id: UUID
    autoheal_event_id: UUID | None
    action_kind: str
    action_payload: dict[str, Any]
    requested_by: UUID
    requested_at: datetime
    state: Literal["pending", "approved", "rejected", "expired"]
    decided_by: UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    expires_at: datetime

    @field_validator("action_payload", mode="before")
    @classmethod
    def _parse_jsonb(cls, v: object) -> object:
        # asyncpg returns JSONB columns as strings without a registered codec
        if isinstance(v, str):
            return json.loads(v)
        return v

    model_config = {"from_attributes": True}
