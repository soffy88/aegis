"""Helpers for at-rest hashing of bearer-style secrets (agent tokens, invite
tokens). We store only sha256(token) so a DB read (backup theft, replica, SQLi
elsewhere) does not yield live credentials; the plaintext leaves the server exactly
once at mint time. Tokens carry full entropy (secrets.token_urlsafe), so a plain
unsalted SHA-256 is sufficient (no offline-guessing risk like human passwords)."""

from __future__ import annotations

import hashlib


def hash_token(raw: str) -> str:
    """Return the hex sha256 of a token for storage / constant-time comparison."""
    return hashlib.sha256(raw.encode()).hexdigest()
