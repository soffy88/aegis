"""Pydantic schemas for C3 error monitoring (error_events + error_issues)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

VALID_LEVELS = Literal["debug", "info", "warning", "error", "fatal"]
VALID_STATES = Literal["unresolved", "resolved", "ignored"]


class ErrorEventCreate(BaseModel):
    org_id: UUID
    project_id: UUID
    fingerprint: str
    ts: datetime | None = None

    exception_type: str = Field(min_length=1, max_length=200)
    exception_value: str | None = Field(default=None, max_length=10000)
    level: VALID_LEVELS = "error"
    environment: str | None = Field(default="prod", max_length=100)
    server_name: str | None = Field(default=None, max_length=200)
    release_name: str | None = Field(default=None, max_length=200)

    stacktrace: dict[str, Any] | None = None
    breadcrumbs: list[dict[str, Any]] | None = None
    user_context: dict[str, Any] | None = None
    tags: dict[str, str] | None = None
    extra: dict[str, Any] | None = None

    sdk_name: str | None = Field(default=None, max_length=100)
    sdk_version: str | None = Field(default=None, max_length=50)
    platform: str | None = Field(default=None, max_length=50)


class ErrorEventResponse(BaseModel):
    event_id: UUID
    issue_id: UUID | None
    org_id: UUID
    project_id: UUID
    fingerprint: str
    ts: datetime

    exception_type: str
    exception_value: str | None
    level: VALID_LEVELS
    environment: str | None
    server_name: str | None
    release_name: str | None

    stacktrace: dict[str, Any] | None
    breadcrumbs: list[dict[str, Any]] | None
    user_context: dict[str, Any] | None
    tags: dict[str, str] | None
    extra: dict[str, Any] | None

    sdk_name: str | None
    sdk_version: str | None
    platform: str | None

    received_at: datetime

    model_config = {"from_attributes": True}


class ErrorIssueResponse(BaseModel):
    issue_id: UUID
    org_id: UUID
    project_id: UUID
    fingerprint: str

    exception_type: str
    exception_value: str | None
    title: str

    event_count: int
    user_count: int

    first_seen: datetime
    last_seen: datetime

    state: VALID_STATES
    first_release: str | None
    last_release: str | None

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
