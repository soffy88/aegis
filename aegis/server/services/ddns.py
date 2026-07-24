"""Dynamic DNS configs (CasaOS remote-access parity).

Stores non-secret DDNS config in ``ddns_configs`` and the credential (duckdns
token / dyndns2 password) encrypted in the secrets vault under
``ddns:<id>:secret``. Refresh delegates to the 3O ``oprim.ddns_update`` primitive
(duckdns / dyndns2), which runs a plain HTTPS call — no host dependency.

Dependency: needs an oprim build shipping ``ddns_update`` (feat/ddns_update →
released tag). Until the aegis oprim pin is bumped, :func:`update_now` raises
:class:`DdnsPrimitiveUnavailable` → the router maps it to 503.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.services import secrets_vault

_PROVIDERS = ("duckdns", "dyndns2")


class DdnsPrimitiveUnavailable(RuntimeError):
    """The installed oprim lacks ddns_update (pin not bumped yet)."""


def _secret_name(config_id: UUID | str) -> str:
    return f"ddns:{config_id}:secret"


def _ddns_update() -> Any:
    try:
        from oprim import ddns_update  # noqa: PLC0415
    except ImportError as e:
        raise DdnsPrimitiveUnavailable(
            "DDNS requires oprim>=3.21 shipping ddns_update; bump the aegis oprim pin"
        ) from e
    return ddns_update


async def create_config(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    provider: str,
    hostname: str,
    secret: str,
    username: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Create a DDNS config; the credential goes to the vault, not the table."""
    if provider not in _PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r} (want one of {_PROVIDERS})")
    if not hostname:
        raise ValueError("hostname is required")
    if not secret:
        raise ValueError("secret (token/password) is required")
    if provider == "dyndns2" and not username:
        raise ValueError("dyndns2 requires a username")

    row = await conn.fetchrow(
        """
        INSERT INTO ddns_configs (org_id, provider, hostname, username, base_url)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, provider, hostname, username, base_url, enabled, created_at
        """,
        org_id,
        provider,
        hostname,
        username,
        base_url,
    )
    await secrets_vault.store_secret(
        conn, org_id=org_id, name=_secret_name(row["id"]), value=secret
    )
    return _public(row)


async def list_configs(conn: asyncpg.Connection, *, org_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, provider, hostname, username, base_url, enabled,
               last_status, last_ip, last_error, last_updated_at, created_at
        FROM ddns_configs WHERE org_id = $1 ORDER BY created_at DESC
        """,
        org_id,
    )
    return [_public(r) for r in rows]


async def delete_config(conn: asyncpg.Connection, *, org_id: UUID, config_id: UUID) -> bool:
    result = await conn.execute(
        "DELETE FROM ddns_configs WHERE id = $1 AND org_id = $2", config_id, org_id
    )
    if result.endswith(" 0"):
        return False
    # Best-effort: drop the vault credential too.
    await conn.execute(
        "DELETE FROM org_secrets WHERE org_id = $1 AND name = $2",
        org_id,
        _secret_name(config_id),
    )
    return True


async def update_now(conn: asyncpg.Connection, *, org_id: UUID, config_id: UUID) -> dict[str, Any]:
    """Push the current IP to the provider and record the outcome."""
    ddns_update = _ddns_update()  # raises DdnsPrimitiveUnavailable if pin not bumped
    cfg = await conn.fetchrow(
        "SELECT id, provider, hostname, username, base_url FROM ddns_configs "
        "WHERE id = $1 AND org_id = $2",
        config_id,
        org_id,
    )
    if cfg is None:
        raise LookupError("ddns config not found")
    secret = await secrets_vault.reveal_secret(conn, org_id=org_id, name=_secret_name(config_id))
    if secret is None:
        raise LookupError("ddns credential missing from vault")

    kwargs: dict[str, Any] = {
        "provider": cfg["provider"],
        "hostname": cfg["hostname"],
        "base_url": cfg["base_url"],
    }
    if cfg["provider"] == "duckdns":
        kwargs["token"] = secret
    else:  # dyndns2
        kwargs["username"] = cfg["username"]
        kwargs["password"] = secret

    result = ddns_update(**kwargs)
    await conn.execute(
        """
        UPDATE ddns_configs
        SET last_status = $3, last_ip = $4,
            last_error = $5, last_updated_at = now()
        WHERE id = $1 AND org_id = $2
        """,
        config_id,
        org_id,
        result.status,
        result.ip,
        None if result.success else result.raw,
    )
    return {
        "success": result.success,
        "changed": result.changed,
        "status": result.status,
        "ip": result.ip,
    }


def _public(row: Any) -> dict[str, Any]:
    d = {
        "id": str(row["id"]),
        "provider": row["provider"],
        "hostname": row["hostname"],
        "username": row["username"],
        "base_url": row["base_url"],
        "enabled": row["enabled"],
        "created_at": row["created_at"].isoformat(),
    }
    if "last_status" in row:
        d.update(
            {
                "last_status": row["last_status"],
                "last_ip": row["last_ip"],
                "last_error": row["last_error"],
                "last_updated_at": (
                    row["last_updated_at"].isoformat() if row["last_updated_at"] else None
                ),
            }
        )
    return d
