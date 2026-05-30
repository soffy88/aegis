"""Pydantic schemas for alert_rules + alert_fired_history."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=200)
    threshold_warn: float | None = None
    threshold_critical: float | None = None
    operator: Literal[">=", ">", "<", "<=", "=="] = ">="
    throttle_seconds: int = Field(default=300, ge=0)
    escalation_delay_seconds: int = Field(default=1800, ge=0)
    dedup_bucket_seconds: int = Field(default=3600, gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def at_least_one_threshold(self) -> AlertRuleCreate:
        if self.threshold_warn is None and self.threshold_critical is None:
            raise ValueError("at least one of threshold_warn / threshold_critical required")
        return self


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    metric: str | None = Field(default=None, min_length=1, max_length=200)
    threshold_warn: float | None = None
    threshold_critical: float | None = None
    operator: Literal[">=", ">", "<", "<=", "=="] | None = None
    throttle_seconds: int | None = Field(default=None, ge=0)
    escalation_delay_seconds: int | None = Field(default=None, ge=0)
    dedup_bucket_seconds: int | None = Field(default=None, gt=0)
    enabled: bool | None = None


class AlertRuleResponse(BaseModel):
    rule_id: UUID
    org_id: UUID
    project_id: UUID
    name: str
    metric: str
    threshold_warn: float | None
    threshold_critical: float | None
    operator: str
    throttle_seconds: int
    escalation_delay_seconds: int
    dedup_bucket_seconds: int
    enabled: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AlertFiredResponse(BaseModel):
    fired_id: UUID
    rule_id: UUID
    org_id: UUID
    project_id: UUID
    dedup_key: str
    severity: Literal["warn", "critical"]
    current_value: float | None
    triggered_reason: str | None
    fired_at: datetime
    escalated_at: datetime | None
    last_seen_at: datetime

    model_config = {"from_attributes": True}
