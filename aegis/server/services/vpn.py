"""Mesh VPN (Tailscale) — CasaOS remote-access parity.

Tailscale runs as a host daemon, so status/up run on the HOST via the privileged
host-shell helper. Status parsing lives in the 3O ``oprim.tailscale_status``
primitive (fed the host's ``tailscale status --json``). ``tailscale up`` is a
host network mutation (R2) — the router gates it on admin+.

ZeroTier/WireGuard are a later slice.

Dependency: needs an oprim build shipping ``tailscale_status``. Until the pin is
bumped, :func:`tailscale_status` returns a not-installed-style shape only when
the CLI is genuinely absent; otherwise it raises VpnPrimitiveUnavailable → 503.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aegis.server.services.host_shell import host_capture, host_exec, sh_quote

_ZT_NETWORK_RE = re.compile(r"^[0-9a-fA-F]{16}$")


class VpnPrimitiveUnavailable(RuntimeError):
    """The installed oprim lacks tailscale_status (pin not bumped yet)."""


def _not_installed() -> dict[str, Any]:
    return {"installed": False, "running": False, "message": "tailscale not installed on host"}


def tailscale_status() -> dict[str, Any]:
    """Return Tailscale status from the host (installed/running/ips/peers)."""
    rc, out, err = host_capture("tailscale status --json")
    if not out.strip():
        # CLI absent or errored with no JSON — report not-installed rather than 500.
        return _not_installed()
    try:
        from oprim import tailscale_status as _ts  # noqa: PLC0415
    except ImportError as e:
        raise VpnPrimitiveUnavailable(
            "VPN status requires oprim>=3.21 shipping tailscale_status; bump the pin"
        ) from e
    return _ts(status_json=out).model_dump()


def tailscale_up(*, authkey: str, accept_routes: bool = False) -> dict[str, Any]:
    """Bring Tailscale up with an auth key (host network mutation, R2).

    Returns the post-up status. Raises RuntimeError if the CLI is absent or the
    command fails.
    """
    if not authkey:
        raise ValueError("authkey is required")
    cmd = f"tailscale up --authkey={sh_quote(authkey)}"
    if accept_routes:
        cmd += " --accept-routes"
    rc, out = host_exec(cmd, timeout=60)
    if rc != 0:
        raise RuntimeError(f"tailscale up failed (rc={rc}): {out[:300]}")
    return tailscale_status()


# ── ZeroTier ─────────────────────────────────────────────────────────────────
#
# Parsed inline (not via an oprim primitive): zerotier-cli -j output is simple
# JSON and this is a cold feature — not worth a shared-lib release cycle.


def _zt_not_installed() -> dict[str, Any]:
    return {"installed": False, "online": False, "message": "zerotier not installed on host"}


def zerotier_status() -> dict[str, Any]:
    """Return ZeroTier node status + joined networks from the host."""
    rc, out, _ = host_capture("zerotier-cli -j info")
    if not out.strip():
        return _zt_not_installed()
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return _zt_not_installed()

    networks: list[dict[str, Any]] = []
    nrc, nout, _ = host_capture("zerotier-cli -j listnetworks")
    if nout.strip():
        try:
            for n in json.loads(nout):
                networks.append(
                    {
                        "id": n.get("nwid") or n.get("id"),
                        "name": n.get("name"),
                        "status": n.get("status"),
                        "addresses": n.get("assignedAddresses") or [],
                    }
                )
        except json.JSONDecodeError:
            pass

    return {
        "installed": True,
        "online": bool(info.get("online")),
        "address": info.get("address"),
        "version": info.get("version"),
        "networks": networks,
        "network_count": len(networks),
    }


def zerotier_join(*, network_id: str) -> dict[str, Any]:
    """Join a ZeroTier network by 16-hex network id (host network mutation, R2)."""
    if not _ZT_NETWORK_RE.match(network_id):
        raise ValueError("network_id must be 16 hex chars")
    rc, out = host_exec(f"zerotier-cli join {sh_quote(network_id)}", timeout=30)
    if rc != 0:
        raise RuntimeError(f"zerotier join failed (rc={rc}): {out[:300]}")
    return zerotier_status()
