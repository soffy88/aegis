"""§5.2 磁盘回收(R2 破坏性,allowlist 硬护栏).

存储守卫越阈时,在 aegis 自有 data_dir 子树内回收可再生文件(临时文件 + 超期日志),
经 oprim.disk_cleanup 的 allowlist 硬约束(target 解析后必须落在 data_dir 内,越界即拒)
保证绝不误删系统路径。R2 破坏性 → 默认 dry_run(只统计),运维显式关闭才真删。

超期自备份由 self_backup.prune_self_backups 单独管,这里不碰,避免双重处理。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _reclaimable_targets(cfg: Any) -> list[str]:
    """枚举 data_dir 下可再生的清理候选(仅 aegis 自有:tmp 内容 + 超期日志文件)。"""
    targets: list[str] = []

    tmp = Path(cfg.data_dir) / "tmp"
    if tmp.is_dir():
        targets += [str(p) for p in tmp.iterdir()]

    logs = Path(cfg.log_dir)
    if logs.is_dir():
        cutoff = time.time() - float(cfg.disk_cleanup_log_age_days) * 86400.0
        for p in logs.glob("*.log*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    targets.append(str(p))
            except OSError:
                continue
    return targets


def reclaim_disk(cfg: Any) -> dict[str, Any]:
    """在 allowlist(data_dir 子树)内回收可再生文件。返回 {freed_bytes,touched,dry_run,targets}。

    同步(文件系统 IO)—— cron 经 to_thread 调。allowlist 越界由 disk_cleanup 抛错拦截。"""
    from oprim import disk_cleanup  # noqa: PLC0415

    targets = _reclaimable_targets(cfg)
    if not targets:
        return {"freed_bytes": 0, "touched": 0, "dry_run": cfg.disk_cleanup_dry_run, "targets": 0}

    # 硬约束:只准触碰 data_dir 子树(resolve 消解符号链接/..);越界 disk_cleanup 直接拒。
    allowlist = [str(Path(cfg.data_dir).resolve())]
    res = disk_cleanup(targets=targets, allowlist=allowlist, dry_run=bool(cfg.disk_cleanup_dry_run))
    return {
        "freed_bytes": res.freed_bytes,
        "touched": len(res.touched_paths),
        "dry_run": res.dry_run,
        "targets": len(targets),
    }
