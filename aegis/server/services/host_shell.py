"""Privileged host-command helper.

Runs a command on the *host* (chroot /host) through the `aegis-host-shell`
privileged helper container — the same helper the host terminal and firewall
router use. aegis-backend itself runs in a container with no host root, so any
host-level probe (lsblk / smartctl / lsusb / mount / …) must go through here.

Powerful — callers must gate on an appropriate RBAC permission and validate
inputs. This module only shells out; it does not decide policy.
"""

from __future__ import annotations

import shlex
import subprocess

from aegis.server.runtime.config import get_settings

_HELPER = "aegis-host-shell"


def _docker_host() -> str:
    return get_settings().docker_host


def _ensure_helper(dh: str) -> None:
    """Ensure the privileged helper container is running (idempotent)."""
    ps = subprocess.run(  # noqa: S603
        ["docker", "-H", dh, "ps", "-q", "-f", f"name=^{_HELPER}$"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if ps.stdout.strip():
        return
    subprocess.run(  # noqa: S603, S607
        ["docker", "-H", dh, "rm", "-f", _HELPER], capture_output=True, check=False
    )
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "-H",
            dh,
            "run",
            "-d",
            "--name",
            _HELPER,
            "--privileged",
            "--pid=host",
            "--network=host",
            "-v",
            "/:/host",
            "--restart",
            "unless-stopped",
            "alpine:latest",
            "sleep",
            "infinity",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def host_exec(cmd: str, *, timeout: int = 25) -> tuple[int, str]:
    """Run `cmd` on the host via chroot; return (returncode, combined stdout+stderr).

    `cmd` is passed to `bash -lc` inside `chroot /host`. Callers MUST shell-escape
    any interpolated, caller-influenced values with :func:`sh_quote`.
    """
    dh = _docker_host()
    _ensure_helper(dh)
    r = subprocess.run(  # noqa: S603
        ["docker", "-H", dh, "exec", _HELPER, "chroot", "/host", "bash", "-lc", cmd],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def host_capture(cmd: str, *, timeout: int = 25) -> tuple[int, str, str]:
    """Like :func:`host_exec` but keeps stdout and stderr separate.

    Returns (returncode, stdout, stderr). Use when stdout must be parsed as
    structured output (JSON) and stderr would corrupt it.
    """
    dh = _docker_host()
    _ensure_helper(dh)
    r = subprocess.run(  # noqa: S603
        ["docker", "-H", dh, "exec", _HELPER, "chroot", "/host", "bash", "-lc", cmd],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return r.returncode, r.stdout, r.stderr.strip()


def sh_quote(value: str) -> str:
    """Shell-escape a single value for safe interpolation into a host command."""
    return shlex.quote(value)
