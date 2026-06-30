"""Audit log — records security-sensitive actions (who did what, when).

`record_audit` is best-effort: an audit-write failure must never abort the action
it is recording (it logs and swallows). Read access is gated by VIEW_AUDIT_LOG.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


async def record_audit(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry. Never raises (best-effort)."""
    try:
        await conn.execute(
            "INSERT INTO audit_log"
            " (org_id, actor_user_id, action, target_type, target_id, metadata)"
            " VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            org_id,
            actor_user_id,
            action,
            target_type,
            target_id,
            json.dumps(metadata or {}),
        )
    except Exception as exc:  # noqa: BLE001 — auditing must not break the action
        log.warning("audit_write_failed action=%s org=%s err=%s", action, org_id, exc)
