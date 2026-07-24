"""File-manager share links (time-limited, capability-token public downloads).

A share is a random high-entropy token that lets anyone download ONE file under
the sandboxed ``file_manager_roots`` without authenticating. The raw token is
returned once at creation and never stored — only its sha256 hash lives in the
DB, so a DB leak can't reconstruct working links. Every download re-validates
the path against the current roots via :func:`files.resolve_for_download`, so a
shrunk whitelist or a deleted file fails closed.

Guardrails: optional expiry, optional max-download count, and revocation.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.services import files as filesvc


class ShareNotValid(Exception):
    """Token unknown, revoked, expired, or over its download limit."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_share(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    path: str,
    created_by: UUID | None,
    expires_in_hours: int | None = None,
    max_downloads: int | None = None,
) -> dict[str, Any]:
    """Create a share for *path*. Validates the path is a real file in the sandbox.

    Returns the row plus the one-time raw ``token`` (never retrievable again).
    """
    # Validate + resolve now so we never mint a link to a disallowed/missing file.
    resolved = filesvc.resolve_for_download(path)  # raises PathNotAllowed / FileNotFoundError
    filename = resolved.name

    token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(UTC) + timedelta(hours=expires_in_hours)
        if expires_in_hours and expires_in_hours > 0
        else None
    )
    if max_downloads is not None and max_downloads <= 0:
        max_downloads = None

    row = await conn.fetchrow(
        """
        INSERT INTO file_shares
            (org_id, token_hash, path, filename, created_by, expires_at, max_downloads)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, filename, expires_at, max_downloads, created_at
        """,
        org_id,
        _hash(token),
        str(resolved),
        filename,
        created_by,
        expires_at,
        max_downloads,
    )
    return {
        "id": str(row["id"]),
        "token": token,  # one-time; caller builds the /s/<token> URL
        "filename": row["filename"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "max_downloads": row["max_downloads"],
        "created_at": row["created_at"].isoformat(),
    }


async def list_shares(conn: asyncpg.Connection, *, org_id: UUID) -> list[dict[str, Any]]:
    """List non-revoked shares for an org (token hashes never exposed)."""
    rows = await conn.fetch(
        """
        SELECT id, path, filename, expires_at, max_downloads, download_count,
               created_at,
               (revoked
                OR (expires_at IS NOT NULL AND expires_at <= now())
                OR (max_downloads IS NOT NULL AND download_count >= max_downloads)
               ) AS expired
        FROM file_shares
        WHERE org_id = $1 AND revoked = FALSE
        ORDER BY created_at DESC
        """,
        org_id,
    )
    return [
        {
            "id": str(r["id"]),
            "path": r["path"],
            "filename": r["filename"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "max_downloads": r["max_downloads"],
            "download_count": r["download_count"],
            "created_at": r["created_at"].isoformat(),
            "active": not r["expired"],
        }
        for r in rows
    ]


async def revoke_share(conn: asyncpg.Connection, *, org_id: UUID, share_id: UUID) -> bool:
    """Revoke a share. Returns True if a row was revoked."""
    result = await conn.execute(
        "UPDATE file_shares SET revoked = TRUE WHERE id = $1 AND org_id = $2 AND revoked = FALSE",
        share_id,
        org_id,
    )
    return result.endswith(" 1")


async def resolve_share(conn: asyncpg.Connection, *, token: str) -> tuple[Path, str]:
    """Validate a presented token and return (file path, filename) for download.

    Atomically bumps the download counter under a row lock so a max-downloads
    limit can't be raced. Re-validates the stored path against the current
    sandbox roots. Raises :class:`ShareNotValid` on any failure.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, path, filename, expires_at, max_downloads, download_count, revoked "
            "FROM file_shares WHERE token_hash = $1 FOR UPDATE",
            _hash(token),
        )
        if row is None or row["revoked"]:
            raise ShareNotValid("share not found or revoked")
        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
            raise ShareNotValid("share expired")
        if row["max_downloads"] is not None and row["download_count"] >= row["max_downloads"]:
            raise ShareNotValid("share download limit reached")

        # Re-validate the path against the CURRENT roots (fail closed if roots
        # shrank or the file was moved/deleted).
        try:
            resolved = filesvc.resolve_for_download(row["path"])
        except Exception as e:  # noqa: BLE001
            raise ShareNotValid("shared file no longer available") from e

        await conn.execute(
            "UPDATE file_shares SET download_count = download_count + 1 WHERE id = $1",
            row["id"],
        )
    return resolved, row["filename"]
