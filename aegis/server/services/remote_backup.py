"""Self-contained remote backup — tar an installed app's data directory and
upload it to S3-compatible storage (AWS S3 / MinIO / any S3 API) via boto3.

This runs entirely in the aegis layer (no dependency on the omodul backup stub),
so app backups actually land in the configured bucket. Credentials come from the
backup_s3_* settings (AWS_* env).
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def is_configured(cfg: Any) -> bool:
    return bool(
        cfg.backup_s3_bucket and cfg.backup_s3_access_key_id and cfg.backup_s3_secret_access_key
    )


def _client(cfg: Any) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.client(
        "s3",
        endpoint_url=cfg.backup_s3_endpoint_url or None,
        aws_access_key_id=cfg.backup_s3_access_key_id,
        aws_secret_access_key=cfg.backup_s3_secret_access_key,
        region_name=cfg.backup_s3_region or "us-east-1",
    )


def test_connection(cfg: Any) -> dict[str, Any]:
    """Check the configured bucket is reachable. Returns {ok, detail}."""
    if not is_configured(cfg):
        return {"ok": False, "detail": "S3 backup storage is not configured"}
    try:
        _client(cfg).head_bucket(Bucket=cfg.backup_s3_bucket)
        return {"ok": True, "detail": f"bucket '{cfg.backup_s3_bucket}' reachable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _tar_dir(src: Path) -> bytes:
    """gzip-tar a directory into memory (app data dirs are small)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(src), arcname=src.name)
    return buf.getvalue()


def backup_app(org_id: str, app_name: str, cfg: Any, data_dir: Path, stamp: str) -> dict[str, Any]:
    """Tar {data_dir}/apps/{app_name} and upload to
    s3://{bucket}/aegis/{org}/{app}/{stamp}.tar.gz. Returns {key, size_bytes, target}.
    """
    if not is_configured(cfg):
        raise RuntimeError("S3 backup storage is not configured")
    src = data_dir / "apps" / app_name
    if not src.exists():
        raise FileNotFoundError(f"no data directory for app '{app_name}' at {src}")
    blob = _tar_dir(src)
    key = f"aegis/{org_id}/{app_name}/{stamp}.tar.gz"
    _client(cfg).put_object(Bucket=cfg.backup_s3_bucket, Key=key, Body=blob)
    log.info("remote_backup uploaded app=%s key=%s size=%d", app_name, key, len(blob))
    return {
        "key": key,
        "size_bytes": len(blob),
        "target": f"s3://{cfg.backup_s3_bucket}/{key}",
    }
