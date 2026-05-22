"""Plan definitions (v0.6 §13.2)."""

from __future__ import annotations

from typing import Any

PLAN_QUOTAS: dict[str, dict[str, Any]] = {
    "free": {
        "projects_max": 1,
        "users_max": 1,
        "storage_gb_monthly": 5,
        "events_monthly": 100_000,
        "llm_tokens_monthly": 0,
        "event_retention_days": 7,
        "autoheal_max_executions_daily": 10,
        "allowed_llm_models": [],
    },
    "indie": {
        "projects_max": 3,
        "users_max": 1,
        "storage_gb_monthly": 50,
        "events_monthly": 1_000_000,
        "llm_tokens_monthly": 100_000,
        "event_retention_days": 30,
        "autoheal_max_executions_daily": 100,
        "allowed_llm_models": ["claude-haiku-4-5"],
    },
    "team": {
        "projects_max": 10,
        "users_max": 20,
        "storage_gb_monthly": 200,
        "events_monthly": 5_000_000,
        "llm_tokens_monthly": 1_000_000,
        "event_retention_days": 90,
        "autoheal_max_executions_daily": 500,
        "allowed_llm_models": ["claude-haiku-4-5", "claude-sonnet-4-6"],
    },
    "business": {
        "projects_max": -1,
        "users_max": 100,
        "storage_gb_monthly": 1000,
        "events_monthly": 25_000_000,
        "llm_tokens_monthly": 10_000_000,
        "event_retention_days": 365,
        "autoheal_max_executions_daily": -1,
        "allowed_llm_models": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
    },
    "enterprise": {
        "projects_max": -1,
        "users_max": -1,
        "storage_gb_monthly": -1,
        "events_monthly": -1,
        "llm_tokens_monthly": -1,
        "event_retention_days": 9999,
        "autoheal_max_executions_daily": -1,
        "allowed_llm_models": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
        "self_hosted": True,
    },
}


def get_quota(plan: str, key: str) -> Any:
    """Get a quota value. -1 means unlimited."""
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS["free"]).get(key, 0)
