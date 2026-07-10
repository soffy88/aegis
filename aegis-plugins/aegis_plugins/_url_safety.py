"""Shared host-allowlist guard for plugins that build outbound URLs from alert data.

Alert payloads are operator/attacker-controlled. Several remediation plugins build
an outbound request URL directly from ``alert_payload`` (e.g. a "management URL" or
"webhook URL" field) and hand it to ``ctx.http_get``. Without a host check, a forged
alert could make the host issue an authenticated request to an arbitrary internal or
external host (SSRF). ``check_url_allowed`` centralizes that guard — call it on every
URL built from alert data, before making the request.

Configured via ``AEGIS_REMEDIATION_ALLOWED_HOSTS`` (colon- or comma-separated
hostnames/IPs, mirroring the ``AEGIS_FILE_MANAGER_ROOTS`` style used by the aegis
server's file-manager allowlist). Fails closed: an empty/unset allowlist rejects
every request rather than silently allowing everything.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS_ENV = "AEGIS_REMEDIATION_ALLOWED_HOSTS"


class UrlNotAllowed(Exception):
    """Target URL's host is outside the configured allowlist (or none is configured)."""


def _allowed_hosts() -> set[str]:
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "")
    parts = [h.strip().lower() for chunk in raw.split(":") for h in chunk.split(",")]
    return {h for h in parts if h}


def check_url_allowed(url: str) -> None:
    """Raise ``UrlNotAllowed`` if *url*'s host isn't in the configured allowlist.

    Fails closed: if ``AEGIS_REMEDIATION_ALLOWED_HOSTS`` is unset/empty, every URL is
    rejected — remediation plugins that hit alert-supplied URLs are disabled until an
    operator configures the allowlist.
    """
    hosts = _allowed_hosts()
    if not hosts:
        logger.warning(
            "%s is not configured; rejecting outbound request to %r "
            "(remediation plugins that call alert-supplied URLs are disabled "
            "until the host allowlist is set)",
            _ALLOWED_HOSTS_ENV,
            url,
        )
        raise UrlNotAllowed(f"{_ALLOWED_HOSTS_ENV} not configured; refusing request to {url!r}")

    host = (urlsplit(url).hostname or "").lower()
    if host not in hosts:
        raise UrlNotAllowed(f"host {host!r} not in {_ALLOWED_HOSTS_ENV} allowlist")
