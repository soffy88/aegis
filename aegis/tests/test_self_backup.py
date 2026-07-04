"""Tests for platform self-DB backup (DESIGN §11.4).

pg_dump 自身控制面 DB → 带校验工件 + 剪枝 + 离宿主上传;cron 到周期才真备份。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron
from aegis.server.services import self_backup


def _cfg(tmp_path, **over):
    c = MagicMock()
    c.backup_local_dir = tmp_path
    c.postgres_dsn = "postgresql://u:p@h:5432/aegisdb"
    c.self_backup_target = over.get("target", "local")
    c.self_backup_retain = over.get("retain", 7)
    c.self_backup_interval_hours = over.get("interval_hours", 24.0)
    c.backup_s3_bucket = over.get("bucket", "")
    c.backup_s3_access_key_id = over.get("akid", "")
    c.backup_s3_secret_access_key = over.get("sk", "")
    return c


def test_database_name_from_dsn():
    assert self_backup._database_name("postgresql://u:p@h:5432/aegisdb") == "aegisdb"
    assert self_backup._database_name("postgresql://u:p@h:5432/") == "aegis"  # 回退


def test_run_self_backup_consumes_backup_database(tmp_path):
    """run_self_backup 应以自身 DSN+库名调 omodul.backup_database,不走 LLM(budget=0)。"""
    captured = {}

    def fake_backup(*, config, input_data, output_dir):
        captured["target"] = config.backup_target
        captured["budget"] = config.budget_usd
        captured["dsn"] = input_data.dsn
        captured["db"] = input_data.database_name
        return {"status": "completed", "findings": None}

    with patch("omodul.backup_database.backup_database", side_effect=fake_backup):
        # 用真实 BackupDatabaseConfig/Input(校验字段),仅替换执行体
        result = self_backup.run_self_backup(_cfg(tmp_path, target="s3-primary"))

    assert result["status"] == "completed"
    assert captured["target"] == "s3-primary"  # ⑥ 注入生效
    assert captured["budget"] == 0.0  # 无 LLM
    assert captured["dsn"].endswith("/aegisdb")
    assert captured["db"] == "aegisdb"


def test_completed_backup_uploads_when_s3_configured(tmp_path):
    artifact = tmp_path / "dump.pgcustom"
    artifact.write_bytes(b"PGDMP")
    findings = MagicMock(
        artifact_path=str(artifact), backup_id="abc", size_bytes=5, checksum_sha256="deadbeef"
    )

    def fake_backup(*, config, input_data, output_dir):
        return {"status": "completed", "findings": findings}

    fake_client = MagicMock()
    with (
        patch("omodul.backup_database.backup_database", side_effect=fake_backup),
        patch("aegis.server.services.remote_backup.is_configured", return_value=True),
        patch("aegis.server.services.remote_backup._client", return_value=fake_client),
    ):
        self_backup.run_self_backup(_cfg(tmp_path, bucket="b", akid="k", sk="s"))

    fake_client.upload_file.assert_called_once()
    args = fake_client.upload_file.call_args.args
    assert args[1] == "b" and args[2].startswith("self/")  # 上传到 self/ 前缀


def test_prune_keeps_only_retain_newest(tmp_path):
    root = tmp_path / "self"
    root.mkdir()
    for name in ["20240101T000000Z", "20240102T000000Z", "20240103T000000Z", "20240104T000000Z"]:
        (root / name).mkdir()
    removed = self_backup.prune_self_backups(_cfg(tmp_path), retain=2)
    assert removed == 2
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["20240103T000000Z", "20240104T000000Z"]  # 保留最新两份


@pytest.mark.asyncio
async def test_self_backup_loop_runs_when_due(tmp_path):
    cron._last_self_backup = None  # 首轮必到期
    calls: list[str] = []

    async def fake_to_thread(fn, *a, **k):
        calls.append(fn.__name__)
        if fn.__name__ == "run_self_backup":
            return {
                "status": "completed",
                "findings": MagicMock(backup_id="x", size_bytes=1, checksum_sha256="ab"),
            }
        return 0

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("aegis.server.runtime.config.get_settings", return_value=_cfg(tmp_path)),
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._self_backup_loop()

    assert "run_self_backup" in calls and "prune_self_backups" in calls
    cron._last_self_backup = None
