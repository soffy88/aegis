"""Pydantic schemas for webhook subscriptions and delivery queue — C2-5."""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

VALID_EVENT_TYPES = {
    "alert.fired",
    "autoheal.completed",
    "autoheal.failed",
    "autoheal.cancelled",
    "release.approved",
    "release.rejected",
    "release.expired",
    "error.new_issue",  # C3-5: new error issue created
    "error.spike",  # C3-5: error rate exceeded threshold
}

# Private/loopback/link-local ranges blocked at webhook URL creation.
# Full DNS-rebinding protection (resolve-at-delivery) is AEGIS-BACKLOG-013 M2.
_BLOCKED_NETWORK_CLASSES = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_reserved",
)


def _validate_webhook_url(v: str) -> str:
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    host = urllib.parse.urlparse(v).hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if any(getattr(addr, attr) for attr in _BLOCKED_NETWORK_CLASSES):
            raise ValueError(f"url host {host!r} is in a blocked address range (SSRF prevention)")
    except ValueError as exc:
        if "blocked address range" in str(exc):
            raise
        # Not a literal IP address — hostname will be checked at delivery time (M2)
    return v


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
        return _validate_webhook_url(v)


class WebhookSubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    secret_encrypted: str | None = Field(default=None, max_length=500)
    event_types: list[str] | None = None
    retry_count: int | None = Field(default=None, ge=0, le=10)
    retry_backoff_seconds: list[int] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_webhook_url(v)

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = set(v) - VALID_EVENT_TYPES
        if invalid:
            raise ValueError(f"unknown event_types: {invalid}")
        return v


class WebhookSubscriptionResponse(BaseModel):
    sub_id: UUID
    org_id: UUID
    name: str
    url: str
    # secret_encrypted is excluded from serialization (write-only).
    # Dispatcher reads it internally; API consumers see only has_secret.
    secret_encrypted: str | None = Field(default=None, exclude=True)
    has_secret: bool = False
    event_types: list[str]
    retry_count: int
    retry_backoff_seconds: list[int]
    enabled: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _derive_has_secret(self) -> WebhookSubscriptionResponse:
        self.has_secret = self.secret_encrypted is not None
        return self

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
