"""Tests for dangerous host storage/power ops — guardrails + dry-run."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aegis.server.services import storage_ops
from aegis.server.services.storage_ops import StorageOpBlocked

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

# sda: clean spare disk (formattable). nvme0n1: the system disk (has "/" mounted).
_TREE = {
    "devices": [
        {
            "path": "/dev/sda",
            "type": "disk",
            "mountpoint": None,
            "children": [{"path": "/dev/sda1", "type": "part", "mountpoint": None}],
        },
        {
            "path": "/dev/nvme0n1",
            "type": "disk",
            "mountpoint": None,
            "children": [{"path": "/dev/nvme0n1p1", "type": "part", "mountpoint": "/"}],
        },
    ]
}


@pytest.fixture(autouse=True)
def stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"exec": [], "audit": []}

    def fake_list(**_k: Any) -> dict[str, Any]:
        return _TREE

    def fake_exec(cmd: str, **_k: Any) -> tuple[int, str]:
        calls["exec"].append(cmd)
        return (0, "")

    async def fake_audit(_conn: Any, **kw: Any) -> None:
        calls["audit"].append(kw)

    monkeypatch.setattr(storage_ops.storage_svc, "list_block_devices", fake_list)
    monkeypatch.setattr(storage_ops, "host_exec", fake_exec)
    monkeypatch.setattr(storage_ops, "record_audit", fake_audit)
    return calls


# ── mount ────────────────────────────────────────────────────────────────────


async def test_mount_dry_run_returns_command() -> None:
    r = await storage_ops.mount(
        None, org_id=_ORG, actor=_USER, device="/dev/sda1", target="/mnt/data"
    )
    assert r["dry_run"] is True
    assert "mount" in r["command"]
    assert "/mnt/data" in r["command"]


async def test_mount_rejects_bad_target() -> None:
    with pytest.raises(StorageOpBlocked, match="/mnt or /media"):
        await storage_ops.mount(None, org_id=_ORG, actor=_USER, device="/dev/sda1", target="/etc/x")


async def test_mount_rejects_unknown_device() -> None:
    with pytest.raises(StorageOpBlocked, match="not found"):
        await storage_ops.mount(None, org_id=_ORG, actor=_USER, device="/dev/zzz", target="/mnt/x")


async def test_mount_real_runs_and_audits(stub: dict[str, Any]) -> None:
    r = await storage_ops.mount(
        None, org_id=_ORG, actor=_USER, device="/dev/sda1", target="/mnt/data", dry_run=False
    )
    assert r["dry_run"] is False
    assert r["mounted"] == "/mnt/data"
    assert stub["exec"]
    assert stub["audit"][0]["action"] == "storage.mount"


async def test_mount_injection_target_blocked() -> None:
    with pytest.raises(StorageOpBlocked):
        await storage_ops.mount(
            None, org_id=_ORG, actor=_USER, device="/dev/sda1", target="/mnt/../etc"
        )


# ── unmount ──────────────────────────────────────────────────────────────────


async def test_unmount_dry_run() -> None:
    r = await storage_ops.unmount(None, org_id=_ORG, actor=_USER, target="/mnt/data")
    assert r["dry_run"] is True
    assert "umount" in r["command"]


async def test_unmount_rejects_bad_target() -> None:
    with pytest.raises(StorageOpBlocked):
        await storage_ops.unmount(None, org_id=_ORG, actor=_USER, target="/")


# ── format (R3) ──────────────────────────────────────────────────────────────


async def test_format_dry_run_on_clean_disk() -> None:
    r = await storage_ops.format_device(
        None, org_id=_ORG, actor=_USER, device="/dev/sda1", fstype="ext4", confirm="/dev/sda1"
    )
    assert r["dry_run"] is True
    assert r["destructive"] is True
    assert r["command"].startswith("mkfs.ext4")


async def test_format_requires_confirm_echo() -> None:
    with pytest.raises(StorageOpBlocked, match="confirmation"):
        await storage_ops.format_device(
            None, org_id=_ORG, actor=_USER, device="/dev/sda1", fstype="ext4", confirm="wrong"
        )


async def test_format_rejects_bad_fstype() -> None:
    with pytest.raises(StorageOpBlocked, match="fstype"):
        await storage_ops.format_device(
            None, org_id=_ORG, actor=_USER, device="/dev/sda1", fstype="ntfs", confirm="/dev/sda1"
        )


async def test_format_refuses_mounted_system_disk() -> None:
    # nvme0n1p1 is "/" — its disk tree is mounted, must refuse.
    with pytest.raises(StorageOpBlocked, match="mounted"):
        await storage_ops.format_device(
            None,
            org_id=_ORG,
            actor=_USER,
            device="/dev/nvme0n1p1",
            fstype="ext4",
            confirm="/dev/nvme0n1p1",
        )


async def test_format_refuses_sibling_of_mounted() -> None:
    # Even a clean partition on a disk with ANY mount is refused (whole-disk check).
    with pytest.raises(StorageOpBlocked, match="mounted"):
        await storage_ops.format_device(
            None,
            org_id=_ORG,
            actor=_USER,
            device="/dev/nvme0n1",
            fstype="ext4",
            confirm="/dev/nvme0n1",
        )


async def test_format_real_runs_and_audits(stub: dict[str, Any]) -> None:
    r = await storage_ops.format_device(
        None,
        org_id=_ORG,
        actor=_USER,
        device="/dev/sda1",
        fstype="xfs",
        confirm="/dev/sda1",
        dry_run=False,
    )
    assert r["formatted"] == "/dev/sda1"
    assert any("mkfs.xfs" in c for c in stub["exec"])
    assert stub["audit"][0]["action"] == "storage.format"


# ── host power (R3) ──────────────────────────────────────────────────────────


async def test_power_dry_run() -> None:
    r = await storage_ops.host_power(
        None, org_id=_ORG, actor=_USER, action="reboot", confirm="reboot"
    )
    assert r["dry_run"] is True
    assert r["command"] == "systemctl reboot"


async def test_power_rejects_bad_action() -> None:
    with pytest.raises(StorageOpBlocked, match="action"):
        await storage_ops.host_power(None, org_id=_ORG, actor=_USER, action="halt", confirm="halt")


async def test_power_requires_confirm() -> None:
    with pytest.raises(StorageOpBlocked, match="confirmation"):
        await storage_ops.host_power(
            None, org_id=_ORG, actor=_USER, action="poweroff", confirm="reboot"
        )


async def test_power_real_audits_before_exec(stub: dict[str, Any]) -> None:
    r = await storage_ops.host_power(
        None, org_id=_ORG, actor=_USER, action="poweroff", confirm="poweroff", dry_run=False
    )
    assert r["action"] == "poweroff"
    assert stub["audit"][0]["action"] == "host.poweroff"
    assert any("systemctl poweroff" in c for c in stub["exec"])
