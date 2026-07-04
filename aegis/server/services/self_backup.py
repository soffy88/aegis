"""平台自身控制面 DB 备份 (DESIGN §11.4).

定时 pg_dump aegis 自身的 postgres(事件/策略/指标/审计),产出带 sha256 校验的可恢复工件,
剪枝旧工件,并在 S3 配了时把工件上传离宿主 —— 本地 dump 抗不了宿主整机故障,而
"运维平台必须比它所管理的东西更可靠",控制面可恢复是底线。

消费 omodul.backup_database(纯 pg_dump,无 LLM)。同步实现,cron 经 to_thread 调。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _database_name(dsn: str) -> str:
    """从 DSN 取库名;取不到回退 'aegis'。"""
    name = urlparse(dsn).path.lstrip("/")
    return name or "aegis"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_self_backup(cfg: Any) -> dict[str, Any]:
    """执行一次自备份:pg_dump → backup_local_dir/self/<ts>/ 下工件 + 校验和 + 报告。

    返回 omodul.backup_database 的结果 dict(findings/status/error/report_path)。
    status='failed'(如 pg_dump 缺失)不抛,由调用方记 error 日志。"""
    from omodul.backup_database import (  # noqa: PLC0415
        BackupDatabaseConfig,
        BackupDatabaseInput,
        backup_database,
    )

    out_dir = Path(cfg.backup_local_dir) / "self" / _stamp()
    conf = BackupDatabaseConfig(
        instance_name="aegis",
        backup_target=cfg.self_backup_target,  # ⑥ 可注入:归档去向标注
        budget_usd=0.0,  # 纯 pg_dump,无 LLM 成本
    )
    inp = BackupDatabaseInput(
        dsn=cfg.postgres_dsn,
        database_name=_database_name(cfg.postgres_dsn),
    )
    result = backup_database(config=conf, input_data=inp, output_dir=out_dir)

    findings = result.get("findings")
    if result.get("status") == "completed" and findings is not None:
        _maybe_upload(cfg, Path(findings.artifact_path))
    return result


def _maybe_upload(cfg: Any, artifact: Path) -> None:
    """S3 配了则把工件上传到 self/ 前缀(离宿主持久化)。best-effort,失败只告警。"""
    from aegis.server.services import remote_backup  # noqa: PLC0415

    if not remote_backup.is_configured(cfg):
        return
    if not artifact.is_file():
        log.warning("self_backup_upload_skip artifact_missing=%s", artifact)
        return
    try:
        key = f"self/{artifact.name}"
        remote_backup._client(cfg).upload_file(str(artifact), cfg.backup_s3_bucket, key)
        log.info("self_backup_uploaded s3://%s/%s", cfg.backup_s3_bucket, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("self_backup_upload_error err=%s", exc)


def prune_self_backups(cfg: Any, retain: int) -> int:
    """只保留最近 retain 份自备份目录,删更旧的。返回删除数。"""
    root = Path(cfg.backup_local_dir) / "self"
    if not root.is_dir():
        return 0
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = 0
    for stale in dirs[max(retain, 0) :]:
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    if removed:
        log.info("self_backup_pruned removed=%d retain=%d", removed, retain)
    return removed
