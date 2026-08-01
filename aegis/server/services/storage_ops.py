"""Dangerous host storage/power mutations — R2/R3, CasaOS parity.

mount / unmount (R2) and format / host-power (R3). These mutate the host, which
DESIGN.md §0.3/I9 marks a non-goal; the user explicitly opted into full CasaOS
parity, so they are implemented — but under strict guardrails matching the risk
grade (DESIGN §5), NOT as blind executors:

  - **dry_run defaults to True**: callers get the exact command without running it.
  - **owner-only** (enforced at the router).
  - **allowlists**: mounts only under /mnt or /media; format only ext4/xfs/btrfs.
  - **confirmation tokens**: format requires echoing the device path; power
    requires echoing the action.
  - **fail-closed**: format refuses a mounted device or the system disk.
  - **audit**: every real (non-dry-run) execution is recorded.

Deliberately NOT placed in the shared oprim library: a `format_device` primitive
callable by any oprim consumer is a footgun, and the value here is the aegis
policy layer, not the trivial shell command.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.persistence.audit import record_audit
from aegis.server.services import storage as storage_svc
from aegis.server.services.host_shell import host_exec, sh_quote

_DEV_RE = re.compile(r"^/dev/[a-zA-Z0-9/_-]{1,64}$")
_MOUNT_ALLOWED_PREFIXES = ("/mnt/", "/media/")
_FORMAT_FSTYPES = {"ext4": "-F", "xfs": "-f", "btrfs": "-f"}
_POWER_ACTIONS = {"reboot", "poweroff"}


class StorageOpBlocked(Exception):
    """A guardrail rejected the operation (invalid input / unsafe target)."""


def _valid_mount_target(target: str) -> bool:
    if "\x00" in target or ".." in target:
        return False
    # Must sit UNDER an allowed prefix (a non-empty subpath after it), so bare
    # "/mnt/" or "/media/" themselves are rejected.
    return any(target.startswith(p) and len(target) > len(p) for p in _MOUNT_ALLOWED_PREFIXES)


def _flatten(devs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in devs:
        out.append(d)
        out.extend(_flatten(d.get("children") or []))
    return out


def _device_and_tree_mountpoints(device: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (matched device node, all mountpoints in that device's top-level tree).

    Uses the host's lsblk (via the storage service). The mountpoints span the whole
    physical disk the device belongs to, so formatting is refused if *anything* on
    that disk is mounted (e.g. the system root).
    """
    data = storage_svc.list_block_devices(include_loop=False)
    tops = data.get("devices", [])
    for top in tops:
        subtree = _flatten([top])
        match = next((n for n in subtree if n.get("path") == device), None)
        if match is not None:
            mps = [n["mountpoint"] for n in subtree if n.get("mountpoint")]
            return match, mps
    return None, []


async def mount(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    actor: UUID | None,
    device: str,
    target: str,
    fstype: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Mount a block device under /mnt or /media (R2). dry_run by default."""
    if not _DEV_RE.match(device):
        raise StorageOpBlocked(f"invalid device path: {device!r}")
    if not _valid_mount_target(target):
        raise StorageOpBlocked("target must be a real path under /mnt or /media")
    node, _ = _device_and_tree_mountpoints(device)
    if node is None:
        raise StorageOpBlocked(f"block device not found: {device}")

    t = "" if not fstype else f"-t {sh_quote(fstype)} "
    cmd = f"mkdir -p {sh_quote(target)} && mount {t}{sh_quote(device)} {sh_quote(target)}"
    if dry_run:
        return {"dry_run": True, "command": cmd}

    rc, out = host_exec(cmd, timeout=30)
    ok = rc == 0
    await record_audit(
        conn,
        org_id=org_id,
        action="storage.mount",
        actor_user_id=actor,
        target_type="device",
        target_id=device,
        metadata={"target": target, "fstype": fstype, "rc": rc, "ok": ok},
    )
    if not ok:
        raise StorageOpBlocked(f"mount failed (rc={rc}): {out[:300]}")
    return {"dry_run": False, "mounted": target, "device": device}


async def unmount(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    actor: UUID | None,
    target: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Unmount a path under /mnt or /media (R2). dry_run by default."""
    if not _valid_mount_target(target):
        raise StorageOpBlocked("target must be a path under /mnt or /media")
    cmd = f"umount {sh_quote(target)}"
    if dry_run:
        return {"dry_run": True, "command": cmd}

    rc, out = host_exec(cmd, timeout=30)
    ok = rc == 0
    await record_audit(
        conn,
        org_id=org_id,
        action="storage.unmount",
        actor_user_id=actor,
        target_type="mountpoint",
        target_id=target,
        metadata={"rc": rc, "ok": ok},
    )
    if not ok:
        raise StorageOpBlocked(f"umount failed (rc={rc}): {out[:300]}")
    return {"dry_run": False, "unmounted": target}


async def format_device(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    actor: UUID | None,
    device: str,
    fstype: str,
    confirm: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a filesystem on a device (R3 — DESTRUCTIVE). dry_run by default.

    Guardrails: fstype allowlist; ``confirm`` must equal the device path; the
    device must exist and NOTHING on its physical disk may be mounted (blocks the
    system disk and any in-use volume).
    """
    if not _DEV_RE.match(device):
        raise StorageOpBlocked(f"invalid device path: {device!r}")
    if fstype not in _FORMAT_FSTYPES:
        raise StorageOpBlocked(f"unsupported fstype {fstype!r} (want {sorted(_FORMAT_FSTYPES)})")
    if confirm != device:
        raise StorageOpBlocked("confirmation must exactly echo the device path")

    node, mountpoints = _device_and_tree_mountpoints(device)
    if node is None:
        raise StorageOpBlocked(f"block device not found: {device}")
    if node.get("type") not in ("disk", "part"):
        raise StorageOpBlocked(f"refusing to format a {node.get('type')} device")
    if mountpoints:
        raise StorageOpBlocked(
            f"refusing to format {device}: its disk has mounted filesystems "
            f"({', '.join(mountpoints)}) — unmount first"
        )

    flag = _FORMAT_FSTYPES[fstype]
    cmd = f"mkfs.{fstype} {flag} {sh_quote(device)}"
    if dry_run:
        return {"dry_run": True, "command": cmd, "destructive": True}

    rc, out = host_exec(cmd, timeout=600)
    ok = rc == 0
    await record_audit(
        conn,
        org_id=org_id,
        action="storage.format",
        actor_user_id=actor,
        target_type="device",
        target_id=device,
        metadata={"fstype": fstype, "rc": rc, "ok": ok},
    )
    if not ok:
        raise StorageOpBlocked(f"mkfs failed (rc={rc}): {out[:300]}")
    return {"dry_run": False, "formatted": device, "fstype": fstype}


async def host_power(
    conn: asyncpg.Connection,
    *,
    org_id: UUID,
    actor: UUID | None,
    action: str,
    confirm: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Reboot or power off the host (R3). dry_run by default.

    This takes Aegis itself down with the box; ``confirm`` must echo the action.
    """
    if action not in _POWER_ACTIONS:
        raise StorageOpBlocked(f"action must be one of {sorted(_POWER_ACTIONS)}")
    if confirm != action:
        raise StorageOpBlocked("confirmation must exactly echo the action")

    cmd = f"systemctl {action}"
    if dry_run:
        return {"dry_run": True, "command": cmd, "destructive": True}

    # Record BEFORE executing — the box may go down before we could audit after.
    await record_audit(
        conn,
        org_id=org_id,
        action=f"host.{action}",
        actor_user_id=actor,
        target_type="host",
        target_id=None,
        metadata={},
    )
    rc, out = host_exec(cmd, timeout=20)
    return {"dry_run": False, "action": action, "rc": rc, "detail": out[:200]}
