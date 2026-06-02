"""Sentry X-Sentry-Auth header parser.

Header format (sent automatically by all Sentry SDKs):
  X-Sentry-Auth: Sentry sentry_version=7, sentry_key=<public_key>,
                 sentry_client=sentry.python/2.0.0, sentry_timestamp=1234567890

Reference: https://develop.sentry.dev/sdk/overview/#authentication
"""

from __future__ import annotations

import re
from typing import NamedTuple


class SentryAuth(NamedTuple):
    public_key: str
    version: str = "7"
    client: str | None = None
    timestamp: str | None = None


class SentryAuthError(ValueError):
    """Raised when X-Sentry-Auth header is missing or malformed."""


def parse_sentry_auth_header(header: str) -> SentryAuth:
    """Parse X-Sentry-Auth header value into a SentryAuth.

    Raises:
        SentryAuthError: header is empty, missing 'Sentry' prefix, or lacks sentry_key.
    """
    if not header:
        raise SentryAuthError("missing X-Sentry-Auth header")

    value = header.strip()
    if value.lower().startswith("sentry "):
        value = value[7:].strip()

    parts: dict[str, str] = {}
    for kv in re.split(r"[,\s]+", value):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        v = v.strip().strip('"')
        if k.startswith("sentry_"):
            parts[k[7:]] = v

    if "key" not in parts:
        raise SentryAuthError("missing sentry_key in X-Sentry-Auth header")

    return SentryAuth(
        public_key=parts["key"],
        version=parts.get("version", "7"),
        client=parts.get("client"),
        timestamp=parts.get("timestamp"),
    )
