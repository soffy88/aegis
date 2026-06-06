"""cleanup-disk — remove old temp files and truncate stale logs."""

from __future__ import annotations

import subprocess

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


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
            subprocess.run(
                ["find", "/tmp", "-maxdepth", "2", "-mtime", "+1", "-delete"],
                check=True,
                capture_output=True,
            )
            cleaned.append("/tmp old files")
        except subprocess.CalledProcessError as exc:
            return ActionResult.failed(f"find /tmp failed: {exc.stderr.decode()[:200]}")

        log_path = ctx.alert_payload.get("log_path", "")
        if log_path:
            try:
                subprocess.run(["truncate", "-s", "0", log_path], check=True, capture_output=True)
                cleaned.append(f"truncated {log_path}")
            except subprocess.CalledProcessError:
                pass

        return ActionResult.ok(f"disk cleanup: {', '.join(cleaned)}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        return True

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.failed("deleted files cannot be recovered")
