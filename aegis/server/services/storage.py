"""Storage / drive observability (read-only, R0).

CasaOS-LocalStorage parity, minimal read-only slice: enumerate the host's block
devices, USB devices, and per-disk SMART health.

Architecture: aegis-backend has no host root, so the raw `lsblk` / `lsusb` /
`smartctl` commands run on the *host* via the privileged host-shell helper
(:mod:`aegis.server.services.host_shell`). The parsing lives in the 3O `oprim`
primitives (`block_device_list` / `usb_device_list` / `disk_smart_probe`),
which accept pre-fetched raw output — execution here, parsing in oprim.

Dependency: needs an oprim build that ships those primitives (feat/storage-observe
→ released tag). Until aegis' oprim pin is bumped, imports are guarded and the
API returns 503 with a clear reason rather than crashing at module load.
"""

from __future__ import annotations

import re
from typing import Any

from aegis.server.services.host_shell import host_capture, sh_quote

# lsblk columns kept in sync with the oprim primitive's expectations.
_LSBLK_COLUMNS = (
    "NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL,"
    "VENDOR,LABEL,UUID,ROTA,TRAN,HOTPLUG,RM,RO,STATE"
)

# A valid /dev device path we are willing to hand to smartctl. Anchored and
# strict to keep caller-influenced values from turning into shell/argument
# injection even though we also shell-escape.
_DEV_RE = re.compile(r"^/dev/[a-zA-Z0-9/_-]{1,64}$")


class StoragePrimitivesUnavailable(RuntimeError):
    """Raised when the installed oprim lacks the storage primitives."""


def _oprim() -> tuple[Any, Any, Any]:
    """Lazy import so this module loads even on an oprim without the primitives."""
    try:
        from oprim import (  # noqa: PLC0415
            block_device_list,
            disk_smart_probe,
            usb_device_list,
        )
    except ImportError as e:  # oprim pin not yet bumped
        raise StoragePrimitivesUnavailable(
            "storage primitives require oprim>=3.21 (block_device_list / "
            "usb_device_list / disk_smart_probe); bump the aegis oprim pin"
        ) from e
    return block_device_list, usb_device_list, disk_smart_probe


def list_block_devices(*, disks_only: bool = False, include_loop: bool = False) -> dict[str, Any]:
    """Enumerate host block devices (disks + partitions) via `lsblk` on the host."""
    block_device_list, _, _ = _oprim()
    rc, out, err = host_capture(f"lsblk -J -b -o {_LSBLK_COLUMNS}")
    if rc != 0 or not out.strip():
        raise RuntimeError(f"lsblk failed on host (rc={rc}): {err[:200]}")
    result = block_device_list(disks_only=disks_only, include_loop=include_loop, lsblk_json=out)
    return result.model_dump()


def list_usb_devices(*, include_root_hubs: bool = False) -> dict[str, Any]:
    """Enumerate host USB devices via `lsusb` on the host."""
    _, usb_device_list, _ = _oprim()
    rc, out, err = host_capture("lsusb")
    if rc != 0:
        # lsusb may be absent on minimal hosts — degrade to empty rather than 500.
        return {"devices": [], "count": 0, "note": f"lsusb unavailable (rc={rc}): {err[:120]}"}
    return usb_device_list(include_root_hubs=include_root_hubs, lsusb_output=out).model_dump()


def probe_smart(*, device: str) -> dict[str, Any]:
    """Read SMART health for one device via `smartctl` on the host."""
    if not _DEV_RE.match(device):
        raise ValueError(f"invalid device path: {device!r}")
    _, _, disk_smart_probe = _oprim()
    rc, out, err = host_capture(f"smartctl -j -H -A -i {sh_quote(device)}")
    if not out.strip():
        # smartctl absent / device has no SMART — return an unavailable-but-valid shape.
        return {
            "device": device,
            "available": False,
            "passed": None,
            "attributes": [],
            "message": f"SMART unavailable (rc={rc}): {err[:120] or 'smartctl produced no output'}",
        }
    # smartctl uses the exit code for SMART status bits; ignore rc, parse the JSON.
    return disk_smart_probe(device=device, smartctl_json=out).model_dump()


def storage_overview(*, include_smart: bool = True) -> dict[str, Any]:
    """Aggregate view for the storage dashboard: disks (+SMART) + USB devices."""
    devices = list_block_devices(disks_only=False, include_loop=False)
    usb = list_usb_devices()

    if include_smart:
        for dev in devices.get("devices", []):
            if dev.get("type") == "disk" and dev.get("path"):
                try:
                    dev["smart"] = probe_smart(device=dev["path"])
                except Exception as e:  # noqa: BLE001 — SMART is best-effort per disk
                    dev["smart"] = {"available": False, "message": str(e)[:120]}

    return {
        "devices": devices.get("devices", []),
        "device_count": devices.get("count", 0),
        "usb": usb.get("devices", []),
        "usb_count": usb.get("count", 0),
    }
