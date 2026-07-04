"""Tests for §5.2 disk reclaim (R2, allowlist hard guard)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from aegis.server.services import disk_reclaim


def _cfg(tmp_path, *, dry_run=True, log_age_days=14):
    c = MagicMock()
    c.data_dir = tmp_path
    c.log_dir = tmp_path / "logs"
    c.disk_cleanup_dry_run = dry_run
    c.disk_cleanup_log_age_days = log_age_days
    return c


def test_reclaimable_targets_tmp_and_stale_logs(tmp_path):
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "junk.bin").write_bytes(b"x")
    (tmp_path / "logs").mkdir()
    old = tmp_path / "logs" / "old.log"
    old.write_text("old")
    fresh = tmp_path / "logs" / "fresh.log"
    fresh.write_text("fresh")
    # 把 old.log mtime 设为 30 天前
    import os

    past = time.time() - 30 * 86400
    os.utime(old, (past, past))

    targets = disk_reclaim._reclaimable_targets(_cfg(tmp_path, log_age_days=14))
    assert str(tmp_path / "tmp" / "junk.bin") in targets
    assert str(old) in targets  # 超期日志入选
    assert str(fresh) not in targets  # 新日志不动


def test_reclaim_disk_dry_run_counts_without_deleting(tmp_path):
    (tmp_path / "tmp").mkdir()
    f = tmp_path / "tmp" / "junk.bin"
    f.write_bytes(b"12345")
    res = disk_reclaim.reclaim_disk(_cfg(tmp_path, dry_run=True))
    assert res["dry_run"] is True
    assert res["targets"] == 1
    assert f.exists()  # dry_run 不删


def test_reclaim_disk_real_delete_within_allowlist(tmp_path):
    (tmp_path / "tmp").mkdir()
    f = tmp_path / "tmp" / "junk.bin"
    f.write_bytes(b"12345")
    res = disk_reclaim.reclaim_disk(_cfg(tmp_path, dry_run=False))
    assert res["dry_run"] is False
    assert not f.exists()  # 真删
    assert res["freed_bytes"] >= 5


def test_no_targets_returns_zero(tmp_path):
    res = disk_reclaim.reclaim_disk(_cfg(tmp_path))
    assert res == {"freed_bytes": 0, "touched": 0, "dry_run": True, "targets": 0}


def test_allowlist_guard_rejects_escape(tmp_path, monkeypatch):
    """target 越出 data_dir → disk_cleanup 抛错(护栏硬约束,拒绝而非跳过)。"""
    from oprim._exceptions import OprimValidationError  # noqa: PLC0415

    # 伪造 _reclaimable_targets 返回一个越界路径(/etc/passwd)
    monkeypatch.setattr(disk_reclaim, "_reclaimable_targets", lambda cfg: ["/etc/passwd"])
    with pytest.raises(OprimValidationError):
        disk_reclaim.reclaim_disk(_cfg(tmp_path, dry_run=True))
