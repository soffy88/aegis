"""Tests for the storage observability service (read-only).

Covers the parse path (host command output -> oprim primitive -> dict), the
graceful-degradation paths, and the primitives-unavailable error mapping.
"""

from __future__ import annotations

import json

import pytest

from aegis.server.services import storage as svc
from aegis.server.services.storage import StoragePrimitivesUnavailable

_FAKE_LSBLK = {
    "blockdevices": [
        {
            "name": "sda",
            "path": "/dev/sda",
            "size": 4000787030016,
            "type": "disk",
            "model": "Elements",
            "serial": "S2",
            "tran": "usb",
            "rota": True,
            "hotplug": True,
            "rm": True,
            "children": [
                {
                    "name": "sda1",
                    "type": "part",
                    "size": 4000000000000,
                    "fstype": "ext4",
                    "mountpoint": "/mnt/data",
                    "rota": True,
                },
            ],
        },
        {"name": "loop0", "type": "loop", "size": 100, "fstype": "squashfs"},
    ]
}
_FAKE_LSUSB = (
    "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
    "Bus 001 Device 004: ID 0781:5567 SanDisk Corp. Cruzer Blade\n"
)
_FAKE_SMART = {
    "device": {"protocol": "ATA"},
    "smart_status": {"passed": True},
    "temperature": {"current": 40},
    "ata_smart_attributes": {
        "table": [
            {"id": 9, "name": "Power_On_Hours", "raw": {"value": 100}},
        ]
    },
}


def _has_primitives() -> bool:
    try:
        from oprim import block_device_list  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


needs_primitives = pytest.mark.skipif(
    not _has_primitives(),
    reason="oprim storage primitives not installed (pin not bumped yet)",
)


@needs_primitives
class TestParsePath:
    def test_list_block_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "host_capture", lambda cmd, **k: (0, json.dumps(_FAKE_LSBLK), ""))
        r = svc.list_block_devices()
        names = [d["name"] for d in r["devices"]]
        assert names == ["sda"]  # loop excluded
        assert r["count"] == 1
        assert r["devices"][0]["transport"] == "usb"
        assert r["devices"][0]["children"][0]["mountpoint"] == "/mnt/data"

    def test_list_usb_devices_excludes_root_hub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "host_capture", lambda cmd, **k: (0, _FAKE_LSUSB, ""))
        r = svc.list_usb_devices()
        assert r["count"] == 1
        assert r["devices"][0]["vendor_id"] == "0781"

    def test_usb_degrades_when_lsusb_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "host_capture", lambda cmd, **k: (127, "", "not found"))
        r = svc.list_usb_devices()
        assert r["devices"] == []
        assert r["count"] == 0
        assert "unavailable" in r["note"]

    def test_probe_smart_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "host_capture", lambda cmd, **k: (0, json.dumps(_FAKE_SMART), ""))
        r = svc.probe_smart(device="/dev/sda")
        assert r["passed"] is True
        assert r["temperature_celsius"] == 40

    def test_probe_smart_absent_smartctl_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "host_capture", lambda cmd, **k: (127, "", "not found"))
        r = svc.probe_smart(device="/dev/sda")
        assert r["available"] is False
        assert "unavailable" in r["message"]

    def test_overview_attaches_smart_per_disk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(cmd: str, **k: object) -> tuple[int, str, str]:
            if cmd.startswith("lsblk"):
                return (0, json.dumps(_FAKE_LSBLK), "")
            if cmd.startswith("lsusb"):
                return (0, _FAKE_LSUSB, "")
            return (0, json.dumps(_FAKE_SMART), "")  # smartctl

        monkeypatch.setattr(svc, "host_capture", fake)
        r = svc.storage_overview()
        assert r["device_count"] == 1
        assert r["usb_count"] == 1
        assert r["devices"][0]["smart"]["passed"] is True


class TestValidationAndDegradation:
    def test_invalid_device_rejected_before_primitives(self) -> None:
        # device validation happens before the oprim import, so this works
        # even when the primitives are absent.
        with pytest.raises(ValueError, match="invalid device path"):
            svc.probe_smart(device="sda; rm -rf /")

    def test_primitives_unavailable_raises_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> None:
            raise StoragePrimitivesUnavailable("needs oprim>=3.21")

        monkeypatch.setattr(svc, "_oprim", boom)
        with pytest.raises(StoragePrimitivesUnavailable):
            svc.list_block_devices()
