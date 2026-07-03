"""Notification channels — outbound alert integrations (Slack / Discord /
Telegram / generic webhook). Broadens beyond raw webhooks toward the integration
breadth of PagerDuty/Datadog for the common chat destinations."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/channels", tags=["channels"])
_KINDS = {"slack", "discord", "telegram", "webhook"}


class ChannelRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    kind: str
    config: dict[str, Any]  # {url} for slack/discord/webhook; {bot_token, chat_id} for telegram


def _send(kind: str, config: dict[str, Any], text: str) -> None:
    if kind in ("slack", "webhook"):
        httpx.post(config["url"], json={"text": text}, timeout=10).raise_for_status()
    elif kind == "discord":
        httpx.post(config["url"], json={"content": text}, timeout=10).raise_for_status()
    elif kind == "telegram":
        httpx.post(
            f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
            json={"chat_id": config["chat_id"], "text": text},
            timeout=10,
        ).raise_for_status()
    else:
        raise ValueError(f"unknown channel kind: {kind}")


@router.get("")
async def list_channels(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, name, kind, enabled, created_at FROM aegis_notification_channels"
        " WHERE org_id = $1 ORDER BY name",
        org_id,
    )
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_channel(
    org_id: uuid.UUID,
    req: ChannelRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, str]:
    import json as _json  # noqa: PLC0415

    if req.kind not in _KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"kind must be one of {sorted(_KINDS)}")
    try:
        cid = await conn.fetchval(
            "INSERT INTO aegis_notification_channels (org_id, name, kind, config)"
            " VALUES ($1,$2,$3,$4) RETURNING id",
            org_id,
            req.name,
            req.kind,
            _json.dumps(req.config),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "channel name exists") from exc
    return {"id": str(cid), "status": "created"}


@router.post("/{channel_id}/test")
async def test_channel(
    org_id: uuid.UUID,
    channel_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, str]:
    import json as _json  # noqa: PLC0415

    row = await conn.fetchrow(
        "SELECT kind, config FROM aegis_notification_channels WHERE id=$1 AND org_id=$2",
        channel_id,
        org_id,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
    cfg = row["config"] if isinstance(row["config"], dict) else _json.loads(row["config"])
    try:
        _send(row["kind"], cfg, "✅ Aegis test notification")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"send failed: {exc}") from exc
    return {"status": "sent"}


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    org_id: uuid.UUID,
    channel_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    await conn.execute(
        "DELETE FROM aegis_notification_channels WHERE id=$1 AND org_id=$2", channel_id, org_id
    )
