"""Account-level auth event trail (login / logout / password change).

Separate from `audit_log` on purpose: audit_log is org-scoped (`org_id NOT NULL`)
while account events either have no org context (a failed login for an unknown
email) or span every org the user belongs to. Writes are best-effort — recording
a login must never be able to break signing in.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg
from fastapi import Request

log = logging.getLogger(__name__)

# Event names are a closed vocabulary so the trail stays queryable.
LOGIN_SUCCEEDED = "login.succeeded"
LOGIN_FAILED = "login.failed"
LOGOUT = "logout"
PASSWORD_CHANGED = "password.changed"
REGISTERED = "register"


def client_info(request: Request | None) -> tuple[str | None, str | None]:
    """Best-effort (ip, user_agent) for *request*.

    Honors X-Forwarded-For's first hop since Aegis runs behind Caddy/cloudflared;
    without that every event would record the reverse proxy's address.
    """
    if request is None:
        return None, None
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    return ip, request.headers.get("user-agent")


async def record_auth_event(
    conn: asyncpg.Connection,
    *,
    event: str,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
    request: Request | None = None,
    detail: str | None = None,
) -> None:
    """Append one auth event. Never raises (best-effort)."""
    ip, ua = client_info(request)
    try:
        await conn.execute(
            "INSERT INTO auth_events (user_id, email, event, ip, user_agent, detail)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            user_id,
            email,
            event,
            ip,
            ua,
            detail,
        )
    except Exception as exc:  # noqa: BLE001 — auditing must not break auth
        log.warning("auth_event_write_failed event=%s email=%s err=%s", event, email, exc)
