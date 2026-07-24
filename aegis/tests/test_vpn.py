"""Tests for mesh-VPN (Tailscale) service + endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.routers import remote_access as ra_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services import vpn as vpn_svc
from aegis.server.services.vpn import VpnPrimitiveUnavailable

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_STATUS_JSON = '{"BackendState":"Running","Self":{"HostName":"box","TailscaleIPs":["100.64.0.1"]}}'


def _has_ts_primitive() -> bool:
    try:
        from oprim import tailscale_status  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


class _FakeStatus:
    def model_dump(self) -> dict[str, Any]:
        return {"installed": True, "running": True, "self_ips": ["100.64.0.1"]}


@pytest.fixture
def with_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake oprim.tailscale_status so the lazy import resolves."""
    import oprim

    monkeypatch.setattr(
        oprim, "tailscale_status", lambda *, status_json: _FakeStatus(), raising=False
    )


# ── service ──────────────────────────────────────────────────────────────────


def test_status_not_installed_when_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (127, "", "not found"))
    r = vpn_svc.tailscale_status()
    assert r["installed"] is False
    assert "not installed" in r["message"]


def test_status_parses_when_primitive_present(
    monkeypatch: pytest.MonkeyPatch, with_primitive: None
) -> None:
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (0, _STATUS_JSON, ""))
    r = vpn_svc.tailscale_status()
    assert r["installed"] is True
    assert r["running"] is True


@pytest.mark.skipif(
    _has_ts_primitive(), reason="oprim tailscale_status present; absent-path not exercised"
)
def test_status_unavailable_when_primitive_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real venv oprim lacks tailscale_status → the lazy import fails → typed error.
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (0, _STATUS_JSON, ""))
    with pytest.raises(VpnPrimitiveUnavailable):
        vpn_svc.tailscale_status()


def test_up_requires_authkey() -> None:
    with pytest.raises(ValueError, match="authkey"):
        vpn_svc.tailscale_up(authkey="")


def test_up_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn_svc, "host_exec", lambda cmd, **k: (1, "auth failed"))
    with pytest.raises(RuntimeError, match="tailscale up failed"):
        vpn_svc.tailscale_up(authkey="tskey-abc")


def test_up_success_returns_status(monkeypatch: pytest.MonkeyPatch, with_primitive: None) -> None:
    monkeypatch.setattr(vpn_svc, "host_exec", lambda cmd, **k: (0, "Success."))
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (0, _STATUS_JSON, ""))
    r = vpn_svc.tailscale_up(authkey="tskey-abc")
    assert r["running"] is True


# ── endpoints ────────────────────────────────────────────────────────────────


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="t@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="org", role="owner")],
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(ra_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


def test_endpoint_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (127, "", "absent"))
    r = client.get(f"/api/v1/orgs/{_ORG}/remote-access/vpn/tailscale")
    assert r.status_code == 200
    assert r.json()["installed"] is False


def test_endpoint_up_validation(client: TestClient) -> None:
    r = client.post(f"/api/v1/orgs/{_ORG}/remote-access/vpn/tailscale/up", json={"authkey": ""})
    assert r.status_code == 400


# ── ZeroTier ─────────────────────────────────────────────────────────────────

_ZT_INFO = '{"address":"deadbeef00","online":true,"version":"1.14.0"}'
_ZT_NETS = (
    '[{"nwid":"8056c2e21c000001","name":"n","status":"OK","assignedAddresses":["10.0.0.2/24"]}]'
)


def test_zt_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn_svc, "host_capture", lambda cmd, **k: (127, "", "absent"))
    assert vpn_svc.zerotier_status()["installed"] is False


def test_zt_status_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def cap(cmd: str, **k: Any) -> tuple[int, str, str]:
        return (0, _ZT_NETS if "listnetworks" in cmd else _ZT_INFO, "")

    monkeypatch.setattr(vpn_svc, "host_capture", cap)
    r = vpn_svc.zerotier_status()
    assert r["installed"] is True
    assert r["online"] is True
    assert r["address"] == "deadbeef00"
    assert r["network_count"] == 1
    assert r["networks"][0]["id"] == "8056c2e21c000001"


def test_zt_join_rejects_bad_id() -> None:
    with pytest.raises(ValueError, match="16 hex"):
        vpn_svc.zerotier_join(network_id="not-hex")


def test_zt_join_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn_svc, "host_exec", lambda cmd, **k: (0, "200 join OK"))
    monkeypatch.setattr(
        vpn_svc,
        "host_capture",
        lambda cmd, **k: (0, _ZT_NETS if "listnetworks" in cmd else _ZT_INFO, ""),
    )
    assert vpn_svc.zerotier_join(network_id="8056c2e21c000001")["installed"] is True


def test_zt_endpoint_join_validation(client: TestClient) -> None:
    r = client.post(
        f"/api/v1/orgs/{_ORG}/remote-access/vpn/zerotier/join", json={"network_id": "xyz"}
    )
    assert r.status_code == 400
