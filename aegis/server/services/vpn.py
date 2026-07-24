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

from typing import Any

from aegis.server.services.host_shell import host_capture, host_exec, sh_quote


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
