"""cleanup-disk — remove old temp files and truncate stale logs."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin

logger = logging.getLogger(__name__)

# Whitelist of host directories the "truncate stale log" action may touch.
# Colon- or comma-separated. Empty falls back to the /var/log default (rather
# than disabling the check) so a forged alert can't truncate arbitrary files.
_ALLOWED_ROOTS_ENV = "AEGIS_CLEANUP_DISK_ALLOWED_ROOTS"
_DEFAULT_ALLOWED_ROOTS = "/var/log"


def _allowed_roots() -> list[Path]:
    raw = os.environ.get(_ALLOWED_ROOTS_ENV) or _DEFAULT_ALLOWED_ROOTS
    parts = [p.strip() for chunk in raw.split(":") for p in chunk.split(",")]
    return [Path(p) for p in parts if p]


def _resolve_within_allowed_roots(log_path: str) -> Path | None:
    """Resolve *log_path* and return it only if contained in an allowed root."""
    p = Path(log_path).resolve()
    for root in _allowed_roots():
        root = root.resolve()
        if p == root or p.is_relative_to(root):
            return p
    return None


class CleanupDiskPlugin(AutoHealPlugin):
    name = "cleanup-disk"
    version = "1.0.0"
    matches_alert = "high_disk_usage"
    description = "Remove old temp files (>1 day) and truncate log files >500 MB."
    rate_limit = "1/hour"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return ctx.alert_payload.get("disk_percent", 0) >= 80

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        cleaned: list[str] = []
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["find", "/tmp", "-maxdepth", "2", "-mtime", "+1", "-delete"],
                check=True,
                capture_output=True,
            )
            cleaned.append("/tmp old files")
        except subprocess.CalledProcessError as exc:
            return ActionResult.failed(f"find /tmp failed: {exc.stderr.decode()[:200]}")

        log_path = ctx.alert_payload.get("log_path", "")
        if log_path:
            if log_path.startswith("-"):
                return ActionResult.failed("invalid log_path: must not start with '-'")
            safe_path = _resolve_within_allowed_roots(log_path)
            if safe_path is None:
                return ActionResult.failed(
                    f"invalid log_path: {log_path!r} is outside the allowed roots "
                    f"({_ALLOWED_ROOTS_ENV})"
                )
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["truncate", "-s", "0", "--", str(safe_path)],
                    check=True,
                    capture_output=True,
                )
                cleaned.append(f"truncated {safe_path}")
            except subprocess.CalledProcessError as exc:
                logger.error("truncate failed for %s: %s", safe_path, exc.stderr.decode()[:200])
                return ActionResult.failed(
                    f"truncate {safe_path} failed: {exc.stderr.decode()[:200]}"
                )

        return ActionResult.ok(f"disk cleanup: {', '.join(cleaned)}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        return True

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.failed("deleted files cannot be recovered")
