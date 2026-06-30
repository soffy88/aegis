"""Encrypted secrets vault — at-rest encryption + rotation (obase crypto).

Secret values are encrypted with a master key before storage and never returned in
plaintext over the API (reads expose only metadata). The dispatcher / webhook signer
can reveal a value internally via ``reveal_secret``. ``rotate_secret`` re-encrypts a
new value and bumps the version.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

import asyncpg
from obase import decrypt_token, encrypt_token

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

_warned_derived_key = False


def master_key(cfg: AegisSettings) -> bytes:
    """32-byte master key: explicit hex if configured, else derived from jwt_secret.

    The derived fallback (sha256 of jwt_secret) couples secret-at-rest encryption to
    the auth signing secret — no domain separation, and only as strong as jwt_secret's
    entropy. Production should set a dedicated 32-byte secrets_master_key. We warn
    once rather than change the derivation, which would orphan already-encrypted rows
    (rotate secrets after setting a dedicated key).
    """
    if cfg.secrets_master_key:
        return bytes.fromhex(cfg.secrets_master_key)
    global _warned_derived_key  # noqa: PLW0603
    if not _warned_derived_key:
        _warned_derived_key = True
        log.warning(
            "secrets_master_key_derived: no dedicated secrets_master_key set — the "
            "vault key is derived from jwt_secret (no domain separation, only as "
            "strong as jwt_secret). Set a 32-byte hex secrets_master_key and rotate "
            "secrets for production-grade at-rest encryption."
        )
    return hashlib.sha256(cfg.jwt_secret.encode()).digest()


async def store_secret(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    name: str,
    value: str,
    cfg: AegisSettings | None = None,
) -> dict[str, Any]:
    """Create or replace a secret (encrypted). Returns metadata (no plaintext)."""
    cfg = cfg or AegisSettings()
    ciphertext = encrypt_token(plaintext=value, master_key=master_key(cfg))
    row = await conn.fetchrow(
        "INSERT INTO org_secrets (org_id, name, ciphertext) VALUES ($1, $2, $3)"
        " ON CONFLICT (org_id, name) DO UPDATE SET"
        "   ciphertext = EXCLUDED.ciphertext,"
        "   version = org_secrets.version + 1,"
        "   rotated_at = now()"
        " RETURNING name, version, created_at, rotated_at",
        org_id,
        name,
        ciphertext,
    )
    return dict(row)


async def rotate_secret(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    name: str,
    new_value: str,
    cfg: AegisSettings | None = None,
) -> dict[str, Any] | None:
    """Re-encrypt a new value + bump version. Returns metadata or None if absent."""
    cfg = cfg or AegisSettings()
    ciphertext = encrypt_token(plaintext=new_value, master_key=master_key(cfg))
    row = await conn.fetchrow(
        "UPDATE org_secrets SET ciphertext = $3, version = version + 1, rotated_at = now()"
        " WHERE org_id = $1 AND name = $2"
        " RETURNING name, version, created_at, rotated_at",
        org_id,
        name,
        ciphertext,
    )
    return dict(row) if row else None


async def reveal_secret(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    name: str,
    cfg: AegisSettings | None = None,
) -> str | None:
    """Decrypt a secret for internal use (never exposed over the API)."""
    cfg = cfg or AegisSettings()
    ciphertext = await conn.fetchval(
        "SELECT ciphertext FROM org_secrets WHERE org_id = $1 AND name = $2", org_id, name
    )
    if ciphertext is None:
        return None
    return decrypt_token(ciphertext=ciphertext, master_key=master_key(cfg))


async def list_secrets(
    conn: asyncpg.Connection, *, org_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Secret metadata only (name/version/timestamps) — never the value."""
    rows = await conn.fetch(
        "SELECT name, version, created_at, rotated_at FROM org_secrets"
        " WHERE org_id = $1 ORDER BY name",
        org_id,
    )
    return [dict(r) for r in rows]
