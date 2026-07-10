"""Invite flow — create, verify, accept org invitations."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.models.membership import Role
from aegis.server.persistence.audit import record_audit
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["invites"])

_INVITE_TTL_DAYS = 7
_INVITEABLE_ROLES = {Role.ADMIN, Role.OPERATOR, Role.MEMBER, Role.VIEWER}


# ── request / response models ─────────────────────────────────────────────────


class CreateInviteRequest(BaseModel):
    email: EmailStr
    role: str


class InviteResponse(BaseModel):
    id: UUID
    token: str
    email: str
    role: str
    org_id: UUID
    expires_at: datetime


class InviteInfoResponse(BaseModel):
    email: str
    role: str
    org_name: str
    expires_at: datetime


class AcceptInviteRequest(BaseModel):
    password: str = Field(..., min_length=8)
    display_name: str | None = None


class AcceptInviteResponse(BaseModel):
    user_id: UUID


# ── helpers ───────────────────────────────────────────────────────────────────


async def _send_invite_email(*, to: str, org_name: str, token: str) -> None:
    cfg = get_settings()
    if not cfg.resend_api_key:
        log.info("invite_email_skipped no_api_key to=%s token=%.8s…", to, token)
        return
    try:
        from obase.email_client.sender import send_notification_email  # noqa: PLC0415

        await send_notification_email(
            to=to,
            title=f"You've been invited to join {org_name} on Aegis",
            body=(
                f"You've been invited to join {org_name}.\n\n"
                f"Accept your invite by visiting /invites/{token} on your Aegis console.\n\n"
                "This invite expires in 7 days."
            ),
            api_key=cfg.resend_api_key,
            from_addr=cfg.email_from_addr,
        )
    except Exception as exc:
        log.warning("invite_email_failed to=%s err=%s", to, exc)


async def _maybe_send_invite_email(
    *, conn: asyncpg.Connection, org_id: UUID, to: str, token: str
) -> None:
    """Fetch org name only when email will actually be sent."""
    cfg = get_settings()
    if not cfg.resend_api_key:
        log.info("invite_email_skipped no_api_key to=%s token=%.8s…", to, token)
        return
    org_row = await conn.fetchrow("SELECT name FROM orgs WHERE id = $1", org_id)
    org_name = org_row["name"] if org_row else str(org_id)
    await _send_invite_email(to=to, org_name=org_name, token=token)


def _check_invite_usable(row: dict) -> None:
    """Raise 410 if invite is expired or already accepted."""
    now = datetime.now(UTC)
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now > expires_at:
        raise HTTPException(status.HTTP_410_GONE, "invite has expired")
    if row["accepted_at"] is not None:
        raise HTTPException(status.HTTP_410_GONE, "invite has already been accepted")


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.post("/api/v1/orgs/{org_id}/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(
    org_id: UUID,
    body: CreateInviteRequest,
    user: UserContext = Depends(require_permission(Permission.INVITE_USER)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> InviteResponse:
    """Create an invite token. admin+ only."""
    try:
        role = Role(body.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid role: {body.role!r}") from exc
    if role not in _INVITEABLE_ROLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"role '{body.role}' cannot be assigned via invite",
        )

    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=_INVITE_TTL_DAYS)

    invite_row = await conn.fetchrow(
        """
        INSERT INTO org_invites (org_id, email, role, token, invited_by, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, token, email, role, org_id, expires_at
        """,
        org_id,
        body.email,
        body.role,
        token,
        user.user_id,
        expires_at,
    )

    await _maybe_send_invite_email(conn=conn, org_id=org_id, to=body.email, token=token)
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="invite.created",
        target_type="invite",
        target_id=str(invite_row["id"]),
        metadata={"email": body.email, "role": body.role},
    )

    # M2-E: token returned so admin can share invite link manually (no email configured).
    # M2-F+ risk: an admin could claim the token themselves to create an account for
    # another email address. Mitigation: once AEGIS_RESEND_API_KEY is set, drop `token`
    # from InviteResponse so the token only leaves the server through the email system.
    return InviteResponse(**dict(invite_row))


@router.get("/api/v1/invites/{token}")
async def get_invite(
    token: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> InviteInfoResponse:
    """Verify invite token and return invite metadata. Public — no auth required."""
    row = await conn.fetchrow(
        """
        SELECT i.email, i.role, i.expires_at, i.accepted_at, o.name AS org_name
        FROM org_invites i
        JOIN orgs o ON o.id = i.org_id
        WHERE i.token = $1
        """,
        token,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")

    _check_invite_usable(dict(row))
    return InviteInfoResponse(
        email=row["email"],
        role=row["role"],
        org_name=row["org_name"],
        expires_at=row["expires_at"],
    )


@router.post("/api/v1/invites/{token}/accept", status_code=status.HTTP_201_CREATED)
async def accept_invite(
    token: str,
    body: AcceptInviteRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> AcceptInviteResponse:
    """Accept invite: create user (if new) + add membership. Public — no auth required."""
    from obase.auth import argon2_hash  # noqa: PLC0415

    invite_row = await conn.fetchrow(
        """
        SELECT i.id, i.org_id, i.email, i.role, i.expires_at, i.accepted_at, o.name AS org_name
        FROM org_invites i
        JOIN orgs o ON o.id = i.org_id
        WHERE i.token = $1
        """,
        token,
    )
    if not invite_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")

    _check_invite_usable(dict(invite_row))

    email: str = invite_row["email"]
    org_id: UUID = invite_row["org_id"]
    role: str = invite_row["role"]
    invite_id: UUID = invite_row["id"]

    # Upsert user — create if not yet registered
    user_row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if user_row is None:
        password_hash = argon2_hash(password=body.password)
        user_row = await conn.fetchrow(
            """
            INSERT INTO users (email, password_hash, display_name)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            email,
            password_hash,
            body.display_name,
        )
    user_id: UUID = user_row["id"]

    # Add membership (idempotent — skip if already a member)
    existing = await conn.fetchrow(
        "SELECT 1 FROM org_memberships WHERE org_id = $1 AND user_id = $2",
        org_id,
        user_id,
    )
    if not existing:
        try:
            await conn.execute(
                """
                INSERT INTO org_memberships (org_id, user_id, role)
                VALUES ($1, $2, $3)
                """,
                org_id,
                user_id,
                role,
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "user already in this org") from exc

    # Mark invite consumed — conditional on not-yet-accepted so two concurrent
    # accepts of the same token can't both succeed (the loser sees 0 rows updated).
    accepted = await conn.fetchval(
        "UPDATE org_invites SET accepted_at = now() WHERE id = $1 AND accepted_at IS NULL"
        " RETURNING id",
        invite_id,
    )
    if accepted is None:
        raise HTTPException(status.HTTP_410_GONE, "invite has already been accepted")
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user_id,
        action="invite.accepted",
        target_type="invite",
        target_id=str(invite_id),
        metadata={"email": email, "role": role},
    )

    return AcceptInviteResponse(user_id=user_id)
