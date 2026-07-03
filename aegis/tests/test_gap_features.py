"""Unit tests for the panel-parity features: remote backup packaging, DB engine
detection, and TLS cert probe error handling."""

from __future__ import annotations

import tarfile
from pathlib import Path

from aegis.server.api.routers.databases import _engine
from aegis.server.api.routers.domains import _probe_cert
from aegis.server.services import remote_backup


class _Cfg:
    backup_s3_bucket = "b"
    backup_s3_endpoint_url = None
    backup_s3_access_key_id = "k"
    backup_s3_secret_access_key = "s"
    backup_s3_region = "us-east-1"


def test_remote_backup_is_configured() -> None:
    assert remote_backup.is_configured(_Cfg) is True

    class Missing(_Cfg):
        backup_s3_bucket = ""

    assert remote_backup.is_configured(Missing) is False


def test_remote_backup_tar_dir(tmp_path: Path) -> None:
    src = tmp_path / "app"
    src.mkdir()
    (src / "data.txt").write_text("hello")
    blob = remote_backup._tar_dir(src)
    import io

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        names = tf.getnames()
    assert "app/data.txt" in names


def test_db_engine_detection() -> None:
    assert _engine("postgres:16-alpine") == "postgres"
    assert _engine("timescale/timescaledb:latest") == "postgres"
    assert _engine("mariadb:11") == "mysql"
    assert _engine("mysql:8") == "mysql"
    assert _engine("redis:7") is None
    assert _engine("nginx") is None


def test_cert_probe_unreachable() -> None:
    r = _probe_cert("nonexistent.invalid.example.test")
    assert r["reachable"] is False
    assert "error" in r
