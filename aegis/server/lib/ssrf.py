"""SSRF guards for server-side fetches of user-supplied URLs.

Thin aegis-layer wrapper over ``oprim.url_safety_check`` (which resolves every
A/AAAA record and tests each IP), with two presets:

- ``guard_external`` — strict. For URLs that should only ever reach the public
  internet (notification channel tests, webhook deliveries). Blocks loopback,
  private, link-local, reserved and multicast.
- ``guard_scrape`` — metadata-safe. For endpoints that legitimately live on
  private/loopback addresses (Prometheus exporters, internal uptime targets) but
  must never reach the cloud metadata service or other reserved space. Blocks
  link-local (169.254.169.254 & friends), reserved and multicast only.

Resolution happens at *fetch* time (call these immediately before the request),
which closes static-internal-hostname SSRF and most DNS-rebinding. A residual
TOCTOU gap remains because the HTTP client re-resolves — pinning the vetted IP
into the request is tracked as a follow-up; this already removes the crown-jewel
vector (reflected cloud-metadata credential theft).
"""

from __future__ import annotations

from oprim import URLSafetyError, url_safety_check


class SSRFBlocked(ValueError):
    """A user-supplied URL resolved into a disallowed address range."""


def _check(url: str, *, block_private: bool, block_loopback: bool) -> None:
    try:
        result = url_safety_check(
            url=url,
            block_loopback=block_loopback,
            block_private=block_private,
            block_link_local=True,
            block_reserved=True,
            block_multicast=True,
        )
    except URLSafetyError as exc:  # unexpected parse/getaddrinfo failure
        raise SSRFBlocked(f"url could not be validated: {exc}") from exc
    if not result.is_safe:
        raise SSRFBlocked(f"url blocked (SSRF prevention): {result.reason}")


def guard_external(url: str) -> None:
    """Reject any URL that resolves to a non-public address. Use for notification
    channels / webhooks that must only reach the public internet."""
    _check(url, block_private=True, block_loopback=True)


def guard_scrape(url: str) -> None:
    """Reject cloud-metadata / link-local / reserved / multicast targets while
    allowing private + loopback exporters. Use for scrape / uptime fetches."""
    _check(url, block_private=False, block_loopback=False)
