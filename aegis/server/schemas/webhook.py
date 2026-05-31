"""Pydantic schemas for webhook subscriptions and delivery queue — C2-5."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

VALID_EVENT_TYPES = {
    "alert.fired",
    "autoheal.completed",
    "autoheal.failed",
    "autoheal.cancelled",
    "release.approved",
    "release.rejected",
    "release.expired",
}


class WebhookSubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    secret_encrypted: str | None = Field(default=None, max_length=500)
    event_types: list[str] = Field(min_length=1)
    retry_count: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: list[int] = Field(default=[5, 15, 45])
    enabled: bool = True

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, v: list[str]) -> list[str]:
        invalid = set(v) - VALID_EVENT_TYPES
        if invalid:
            raise ValueError(f"unknown event_types: {invalid}")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class WebhookSubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    secret_encrypted: str | None = Field(default=None, max_length=500)
    event_types: list[str] | None = None
    retry_count: int | None = Field(default=None, ge=0, le=10)
    retry_backoff_seconds: list[int] | None = None
    enabled: bool | None = None


class WebhookSubscriptionResponse(BaseModel):
    sub_id: UUID
    org_id: UUID
    name: str
    url: str
    secret_encrypted: str | None
    event_types: list[str]
    retry_count: int
    retry_backoff_seconds: list[int]
    enabled: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookDeliveryResponse(BaseModel):
    delivery_id: UUID
    sub_id: UUID
    org_id: UUID
    event_type: str
    payload: dict[str, Any]
    attempt_no: int
    max_attempts: int
    next_attempt_at: datetime
    last_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    state: Literal["pending", "in_flight", "succeeded", "failed", "dead_letter"]
    created_at: datetime
    succeeded_at: datetime | None

    @field_validator("payload", mode="before")
    @classmethod
    def _parse_jsonb(cls, v: object) -> object:
        # asyncpg returns JSONB columns as strings without a registered codec
        if isinstance(v, str):
            return json.loads(v)
        return v

    model_config = {"from_attributes": True}
