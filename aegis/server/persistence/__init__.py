"""Persistence layer."""

from __future__ import annotations

from aegis.server.persistence.db import (
    acquire,
    close_pool,
    get_pool,
    init_pool,
)
from aegis.server.persistence.event_trail import (
    EventType,
    append_event,
    causal_chain,
    recent_events,
)
from aegis.server.persistence.migrations import apply_migrations

__all__ = [
    "EventType",
    "acquire",
    "append_event",
    "apply_migrations",
    "causal_chain",
    "close_pool",
    "get_pool",
    "init_pool",
    "recent_events",
]
