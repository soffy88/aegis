"""On-call rotation — compute who is on call now and route notifications.

A schedule is a fixed rotation of users plus a shift length and an anchor time.
current index = floor((now - anchor) / shift_length) % len(rotation).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import asyncpg

log = logging.getLogger(__name__)


def whose_shift(
    rotation: list[uuid.UUID],
    *,
    shift_length_seconds: int,
    anchor_at: datetime,
    now: datetime,
) -> uuid.UUID | None:
    """Pure rotation math — returns the on-call user, or None if rotation is empty."""
    if not rotation or shift_length_seconds <= 0:
        return None
    elapsed = (now - anchor_at).total_seconds()
    index = int(elapsed // shift_length_seconds) % len(rotation)
    return rotation[index]


async def current_oncall(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Return the user currently on call for the org's first enabled schedule."""
    now = now or datetime.now(UTC)
    row = await conn.fetchrow(
        "SELECT rotation, shift_length_seconds, anchor_at FROM oncall_schedules"
        " WHERE org_id = $1 AND enabled = TRUE ORDER BY created_at ASC LIMIT 1",
        org_id,
    )
    if row is None:
        return None
    return whose_shift(
        list(row["rotation"]),
        shift_length_seconds=row["shift_length_seconds"],
        anchor_at=row["anchor_at"],
        now=now,
    )
