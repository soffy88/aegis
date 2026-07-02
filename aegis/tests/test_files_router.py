"""Tests for the host file-manager router + service (sandbox + CRUD)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.routers import files as files_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.runtime import config as cfg

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _base() -> str:
    return f"/api/v1/orgs/{_ORG}/files"


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure a single sandbox root at tmp_path and clear the settings cache."""
    monkeypatch.setenv("AEGIS_FILE_MANAGER_ROOTS", str(tmp_path))
    cfg.get_settings.cache_clear()
    yield tmp_path
    cfg.get_settings.cache_clear()


@pytest.fixture
def client(root: Path) -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(files_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


def test_roots_lists_configured(client: TestClient, root: Path) -> None:
    r = client.get(f"{_base()}/roots")
    assert r.status_code == 200
    assert r.json()["roots"] == [str(root)]


def test_list_and_navigation(client: TestClient, root: Path) -> None:
    (root / "sub").mkdir()
    (root / "a.txt").write_text("hello")
    r = client.get(f"{_base()}/list", params={"path": str(root)})
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert names == ["sub", "a.txt"]  # dirs first, then files
    assert body["parent"] is None  # root has no parent link


def test_outside_root_is_forbidden(client: TestClient) -> None:
    r = client.get(f"{_base()}/list", params={"path": "/etc"})
    assert r.status_code == 403


def test_traversal_is_forbidden(client: TestClient, root: Path) -> None:
    r = client.get(f"{_base()}/list", params={"path": f"{root}/../../etc"})
    assert r.status_code == 403


def test_write_read_roundtrip(client: TestClient, root: Path) -> None:
    target = str(root / "note.txt")
    w = client.put(f"{_base()}/write", json={"path": target, "content": "abc\n"})
    assert w.status_code == 200
    rd = client.get(f"{_base()}/read", params={"path": target})
    assert rd.status_code == 200
    assert rd.json()["content"] == "abc\n"


def test_write_refuses_missing_parent(client: TestClient, root: Path) -> None:
    r = client.put(
        f"{_base()}/write",
        json={"path": str(root / "nope" / "x.txt"), "content": "x"},
    )
    assert r.status_code == 404


def test_mkdir_upload_rename_delete(client: TestClient, root: Path) -> None:
    d = str(root / "d")
    assert client.post(f"{_base()}/mkdir", json={"path": d}).status_code == 201

    up = client.post(
        f"{_base()}/upload",
        data={"dir": d},
        files={"file": ("u.bin", b"\x00\x01", "application/octet-stream")},
    )
    assert up.status_code == 201
    assert (root / "d" / "u.bin").read_bytes() == b"\x00\x01"

    rn = client.post(
        f"{_base()}/rename",
        json={"src": f"{d}/u.bin", "dst": f"{d}/renamed.bin"},
    )
    assert rn.status_code == 200
    assert (root / "d" / "renamed.bin").exists()

    dl = client.request("DELETE", f"{_base()}/delete", params={"path": d})
    assert dl.status_code == 200
    assert not (root / "d").exists()


def test_delete_refuses_root(client: TestClient, root: Path) -> None:
    r = client.request("DELETE", f"{_base()}/delete", params={"path": str(root)})
    assert r.status_code == 403


def test_disabled_when_no_roots(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_FILE_MANAGER_ROOTS", "")
    cfg.get_settings.cache_clear()
    # roots endpoint returns empty (feature-off signal for the UI)
    assert client.get(f"{_base()}/roots").json()["roots"] == []
    # an actual operation is rejected as unavailable
    r = client.get(f"{_base()}/list", params={"path": "/tmp"})
    assert r.status_code == 503
