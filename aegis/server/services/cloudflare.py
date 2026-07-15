"""Cloudflare API integration for the "Publish" feature.

Publishing an already-running internal service means adding a hostname to the
*same* Cloudflare Tunnel aegis-cloudflared already runs (see docker-compose.aegis.yml
+ CLOUDFLARED_TOKEN) — no new tunnel, no new account/tunnel id to configure.
`parse_tunnel_token` decodes the existing token (cloudflared itself parses it the
same way) to recover account_id/tunnel_id, then the rest of this module talks to
the Cloudflare API directly (Tunnel Configuration + DNS) using a *separate*,
org-scoped API token (stored in the secrets vault as "cloudflare_api_token" —
different credential, narrower blast radius than the tunnel token itself).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareApiError(Exception):
    """Cloudflare API call failed (non-2xx, unreachable, or success=false)."""


def parse_tunnel_token(token: str) -> tuple[str, str]:
    """Decode a cloudflared tunnel token into (account_id, tunnel_id).

    The token is base64 of a JSON object `{"a": account_id, "t": tunnel_id, "s":
    secret}` — the same thing `cloudflared tunnel run --token` decodes internally.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.b64decode(padded))
        return payload["a"], payload["t"]
    except (binascii.Error, ValueError, KeyError) as exc:
        raise CloudflareApiError(f"malformed CLOUDFLARED_TOKEN: {exc}") from exc


def _headers(api_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}


async def _call(
    client: httpx.AsyncClient, method: str, url: str, api_token: str, **kwargs: Any
) -> dict[str, Any]:
    try:
        resp = await client.request(method, url, headers=_headers(api_token), **kwargs)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CloudflareApiError(
            f"cloudflare api {method} {url} -> {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise CloudflareApiError(f"cloudflare api unreachable: {exc}") from exc
    body = resp.json()
    if not body.get("success", False):
        raise CloudflareApiError(
            f"cloudflare api {method} {url} returned success=false: {body.get('errors')}"
        )
    return body


async def get_zone_id(api_token: str, root_domain: str, *, timeout_sec: float = 10.0) -> str:
    """Resolve a Cloudflare zone id for e.g. "kanpan.co"."""
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        body = await _call(
            client, "GET", f"{_API_BASE}/zones", api_token, params={"name": root_domain}
        )
    results = body.get("result") or []
    if not results:
        raise CloudflareApiError(f"no Cloudflare zone found for {root_domain!r}")
    return results[0]["id"]


async def add_public_hostname(
    api_token: str,
    account_id: str,
    tunnel_id: str,
    hostname: str,
    service: str,
    *,
    timeout_sec: float = 10.0,
) -> None:
    """Add (or replace) a hostname -> service ingress rule on the tunnel.

    Fetches the current ingress rule list, drops any existing rule for this
    hostname (idempotent re-publish), inserts the new rule before the trailing
    catch-all, and writes the whole config back atomically (Cloudflare has no
    single-rule PATCH for tunnel ingress).
    """
    url = f"{_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        current = await _call(client, "GET", url, api_token)
        ingress: list[dict[str, Any]] = (
            current.get("result", {}).get("config", {}).get("ingress", [])
        )
        rules = [r for r in ingress if r.get("hostname") != hostname]
        catch_all = [r for r in rules if not r.get("hostname")]
        named = [r for r in rules if r.get("hostname")]
        new_ingress = [
            *named,
            {"hostname": hostname, "service": service},
            *(catch_all or [{"service": "http_status:404"}]),
        ]
        await _call(client, "PUT", url, api_token, json={"config": {"ingress": new_ingress}})
    log.info("cloudflare_hostname_added hostname=%s service=%s", hostname, service)


async def remove_public_hostname(
    api_token: str, account_id: str, tunnel_id: str, hostname: str, *, timeout_sec: float = 10.0
) -> None:
    """Remove a hostname's ingress rule from the tunnel. No-op if already absent."""
    url = f"{_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        current = await _call(client, "GET", url, api_token)
        ingress: list[dict[str, Any]] = (
            current.get("result", {}).get("config", {}).get("ingress", [])
        )
        new_ingress = [r for r in ingress if r.get("hostname") != hostname]
        await _call(client, "PUT", url, api_token, json={"config": {"ingress": new_ingress}})
    log.info("cloudflare_hostname_removed hostname=%s", hostname)


async def ensure_dns_record(
    api_token: str, zone_id: str, hostname: str, tunnel_id: str, *, timeout_sec: float = 10.0
) -> str:
    """Create a proxied CNAME hostname -> <tunnel_id>.cfargotunnel.com if absent.

    Idempotent: returns the existing record id if one already matches.
    """
    target = f"{tunnel_id}.cfargotunnel.com"
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        existing = await _call(
            client,
            "GET",
            f"{_API_BASE}/zones/{zone_id}/dns_records",
            api_token,
            params={"type": "CNAME", "name": hostname},
        )
        results = existing.get("result") or []
        if results:
            return results[0]["id"]
        created = await _call(
            client,
            "POST",
            f"{_API_BASE}/zones/{zone_id}/dns_records",
            api_token,
            json={"type": "CNAME", "name": hostname, "content": target, "proxied": True},
        )
    record_id = created["result"]["id"]
    log.info("cloudflare_dns_record_created hostname=%s record_id=%s", hostname, record_id)
    return record_id


async def delete_dns_record(
    api_token: str, zone_id: str, record_id: str, *, timeout_sec: float = 10.0
) -> None:
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        await _call(
            client, "DELETE", f"{_API_BASE}/zones/{zone_id}/dns_records/{record_id}", api_token
        )
    log.info("cloudflare_dns_record_deleted record_id=%s", record_id)
