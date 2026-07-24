"""Tests for file-manager image thumbnails (service + endpoint)."""

from __future__ import annotations

import io
import shutil
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from aegis.server.api.routers import files as files_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services import files as filesvc
from aegis.server.services.files import ThumbnailUnavailable

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _has_primitive() -> bool:
    try:
        from oprim import thumbnail_generate  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


needs_primitive = pytest.mark.skipif(
    not _has_primitive(), reason="oprim thumbnail_generate not installed (pin not bumped)"
)


@pytest.fixture
def png_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "pic.png"
    Image.new("RGB", (800, 400), (10, 120, 200)).save(f, format="PNG")
    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", lambda _p: f)
    return f


# ── service ──────────────────────────────────────────────────────────────────


def test_thumbnail_unavailable_without_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    if _has_primitive():
        pytest.skip("primitive present; unavailable path not exercised")
    with pytest.raises(ThumbnailUnavailable):
        filesvc.generate_thumbnail("/whatever.png")


@needs_primitive
def test_generate_thumbnail_webp(png_file: Path) -> None:
    data, media = filesvc.generate_thumbnail(str(png_file), max_size=256)
    assert media == "image/webp"
    out = Image.open(io.BytesIO(data))
    assert out.format == "WEBP"
    assert max(out.size) == 256  # 800x400 → 256x128


@needs_primitive
def test_generate_thumbnail_rejects_non_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "notimg.txt"
    f.write_text("hello")
    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", lambda _p: f)
    with pytest.raises(ValueError, match="not a decodable image"):
        filesvc.generate_thumbnail(str(f))


# ── endpoint ─────────────────────────────────────────────────────────────────


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="t@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="org", role="owner")],
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(files_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


@needs_primitive
def test_endpoint_returns_webp(client: TestClient, png_file: Path) -> None:
    r = client.get(f"/api/v1/orgs/{_ORG}/files/thumbnail", params={"path": str(png_file)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).format == "WEBP"


def test_endpoint_503_without_primitive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_p: str, **_k: object) -> tuple[bytes, str]:
        raise ThumbnailUnavailable("needs oprim[image]>=3.21")

    monkeypatch.setattr(filesvc, "generate_thumbnail", boom)
    r = client.get(f"/api/v1/orgs/{_ORG}/files/thumbnail", params={"path": "/x.png"})
    assert r.status_code == 503


# ── video / media (needs ffmpeg + oprim>=3.22) ───────────────────────────────

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _has_media_primitive() -> bool:
    try:
        from oprim import video_thumbnail  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


needs_media = pytest.mark.skipif(
    not (_HAS_FFMPEG and _has_media_primitive()),
    reason="ffmpeg or oprim>=3.22 media primitives not available",
)


@pytest.fixture
def sample_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import subprocess

    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=5",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        timeout=30,
    )
    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", lambda _p: out)
    return out


@needs_media
def test_video_thumbnail_returns_jpeg(sample_video: Path) -> None:
    data, media = filesvc.generate_thumbnail(str(sample_video), max_size=160)
    assert media == "image/jpeg"
    assert data[:2] == b"\xff\xd8"  # JPEG SOI


@needs_media
def test_probe_media_video(sample_video: Path) -> None:
    info = filesvc.probe_media(str(sample_video))
    assert info["is_video"] is True
    assert info["width"] == 320
    assert info["height"] == 240
