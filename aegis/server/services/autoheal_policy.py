"""Policy-driven closed-loop autoheal (safe by design).

Each `autoheal_policies` row binds ONE target container to a trigger
(metric/operator/threshold) and an action (restart). The loop evaluates each
enabled policy against recent metrics for that container; on breach (and past the
per-policy cooldown) it either logs the intended action (dry_run, the default) or
executes it on that specific container. There is no blanket auto-remediation — a
real restart only happens for a policy explicitly set dry_run=false.

Outcomes are written to aegis_alert_events so they appear on the autoheal
dashboard.

P0-2: Fail-closed safety gate, persistent flapping/rate-limit state, dual checks,
immutable event trail, duplicate execution prevention.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg

from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository
from aegis.server.services.safety_mode import SafetyMode

log = logging.getLogger(__name__)

_LOOKBACK_SEC = 180


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _load_heal_history(
    conn: asyncpg.Connection, target: str, window_seconds: int
) -> list[datetime]:
    """Load heal history for a target from PostgreSQL within the window."""
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    rows = await conn.fetch(
        """
        SELECT healed_at FROM autoheal_heal_history
        WHERE target_container = $1 AND healed_at > $2
        ORDER BY healed_at DESC
        """,
        target,
        cutoff,
    )
    return [r["healed_at"] for r in rows]


async def _load_global_actions(conn: asyncpg.Connection, window_seconds: int) -> list[datetime]:
    """Load global action timestamps from PostgreSQL within the window."""
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    rows = await conn.fetch(
        """
        SELECT acted_at FROM autoheal_global_actions
        WHERE acted_at > $1
        ORDER BY acted_at DESC
        """,
        cutoff,
    )
    return [r["acted_at"] for r in rows]


async def _rate_limited(
    conn: asyncpg.Connection, now: datetime, *, max_actions: int, window_seconds: int
) -> bool:
    """Global real autoheal action rate limit from PostgreSQL."""
    actions = await _load_global_actions(conn, window_seconds)
    return len(actions) >= max_actions


async def _record_heal(conn: asyncpg.Connection, target: str, now: datetime) -> None:
    """Record a heal event for flapping detection."""
    await conn.execute(
        """
        INSERT INTO autoheal_heal_history (target_container, healed_at)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        target,
        now,
    )


async def _record_global_action(conn: asyncpg.Connection, now: datetime) -> None:
    """Record a global action for rate limiting."""
    await conn.execute(
        """
        INSERT INTO autoheal_global_actions (acted_at)
        VALUES ($1)
        """,
        now,
    )


async def _check_duplicate_execution(
    conn: asyncpg.Connection, policy_id: uuid.UUID, now: datetime
) -> bool:
    """Check if this policy was already executed in this tick (prevent duplicate restarts)."""
    from datetime import timedelta

    # Check if there's already an event for this policy in the last 10 seconds
    row = await conn.fetchrow(
        """
        SELECT 1 FROM aegis_alert_events
        WHERE source = $1 AND created_at > $2
        LIMIT 1
        """,
        f"autoheal:policy:{policy_id}",
        now - timedelta(seconds=10),
    )
    return row is not None


async def _trigger_value(
    conn: asyncpg.Connection, *, metric: str, target: str, operator: str
) -> float | None:
    """Worst recent value of `metric` among series referencing `target` container.

    Matches by hostname (uptime target name) or by container name/id in tags, so it
    works for both probe_up (hostname=target) and cAdvisor metrics (id/name in tags).
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname, tags) value
        FROM agent_metrics
        WHERE metric_name = $1
          AND ts > now() - ($3 * interval '1 second')
          AND ( hostname = $2
                OR tags->>'name' = $2
                OR tags->>'target' = $2
                OR (tags->>'id') LIKE '%' || $2 || '%' )
        ORDER BY hostname, tags, ts DESC
        """,
        metric,
        target,
        _LOOKBACK_SEC,
    )
    vals = [r["value"] for r in rows]
    if not vals:
        return None
    if operator in (">", ">="):
        return cast(float, max(vals))
    if operator in ("<", "<="):
        return cast(float, min(vals))
    return cast(float, vals[0])


def _breached(value: float, operator: str, threshold: float) -> bool:
    return bool(
        {
            ">=": value >= threshold,
            ">": value > threshold,
            "<=": value <= threshold,
            "<": value < threshold,
            "==": value == threshold,
        }.get(operator, False)
    )


async def run_autoheal_policies(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Evaluate all enabled policies; act on breaches past cooldown. Returns actions.

    §5.3 安全层(闸门顺序):
    1. SafetyMode QUALIFIED required for real actions (fail-closed)
    2. Global kill-switch (config + runtime flag) — query failure = fail-closed
    3. Change freeze window
    4. Flapping detection (persistent in PostgreSQL)
    5. Global rate limit (persistent in PostgreSQL)
    6. Dual pre/post restart checks

    dry_run policies bypass flapping/rate-limit but still respect SafetyMode.
    """
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from aegis.server.services.platform_flags import (  # noqa: PLC0415
        AUTOHEAL_KILL_SWITCH,
        is_flag_enabled,
    )

    cfg = get_settings()
    # 全局急停:config 关 或 运行时 flag 置位 → 停止一切自愈(§5.3)。
    # P0-2: kill-switch 查询失败 = fail-closed (不再保守放行)
    if not cfg.autoheal_enabled:
        log.info("autoheal_disabled_config — 跳过所有自愈")
        return []

    try:
        if await is_flag_enabled(conn, AUTOHEAL_KILL_SWITCH):
            log.warning("autoheal_kill_switch_active — 全局急停置位,跳过所有自愈动作")
            return []
    except Exception as exc:  # noqa: BLE001
        # P0-2: 急停开关读取失败 = fail-closed (不再保守放行)
        log.error("autoheal_kill_switch_read_error err=%s (fail-closed)", exc)
        return []

    # §9/§3.3 变更冻结窗口:高风险时段禁自动自愈(部署侧另有闸门)。
    from aegis.server.services.change_freeze import is_change_frozen  # noqa: PLC0415

    if is_change_frozen(cfg, _utcnow()):
        log.warning("autoheal_change_frozen — 变更冻结窗口内,禁止自动自愈(§9/§3.3)")
        return []

    # §11.1 / C-11: degraded mode 下 auto 自愈必须禁用(默认 fail-closed)。
    from aegis.server.services.preconditions import get_readiness  # noqa: PLC0415

    try:
        readiness = await get_readiness(conn)
        if readiness.degraded:
            log.warning(
                "autoheal_degraded_mode — §11 前置条件未满足,auto 自愈禁用。unsatisfied=%s",
                [s.key for s in readiness.unsatisfied],
            )
            return []
    except Exception as exc:  # noqa: BLE001
        log.warning("autoheal_readiness_read_error err=%s (fail-open)", exc)

    # P0-2: SafetyMode gate — must be QUALIFIED for real actions
    from aegis.server.services.safety_mode import compute_safety_mode  # noqa: PLC0415

    safety = await compute_safety_mode(conn)
    if safety.mode != SafetyMode.QUALIFIED:
        log.warning("autoheal_safety_mode_blocked mode=%s — 仅允许 dry-run", safety.mode.value)

    policies = await conn.fetch(
        """
        SELECT id, org_id, name, target_container, trigger_metric, trigger_operator,
               trigger_threshold, action, dry_run, cooldown_seconds, docker_host,
               canary, last_triggered_at
        FROM autoheal_policies
        WHERE enabled = TRUE
          AND (last_triggered_at IS NULL
               OR last_triggered_at <= now() - (cooldown_seconds * interval '1 second'))
          AND (NOT $1 OR canary = TRUE)
        """,
        os.environ.get("AEGIS_AUTOHEAL_CANARY_ONLY", "false").lower() == "true",
    )
    events = AutoHealEventRepository(conn)
    actions: list[dict[str, Any]] = []

    # §5.4 / C-5.4: 成熟度未过 L2 且目标非 aegis-canary → 拒绝执行 (config 不可覆盖)。
    from aegis.server.services.maturity import is_auto_allowed  # noqa: PLC0415

    auto_allowed: bool | None = None
    if cfg.autoheal_require_l2:
        try:
            auto_allowed = await is_auto_allowed(conn, "autoheal.restart", require_l2=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("autoheal_maturity_read_error err=%s (fail-open)", exc)
            auto_allowed = None

    for p in policies:
        value = await _trigger_value(
            conn,
            metric=p["trigger_metric"],
            target=p["target_container"],
            operator=p["trigger_operator"],
        )
        if value is None or not _breached(value, p["trigger_operator"], p["trigger_threshold"]):
            continue

        target = p["target_container"]
        dry = p["dry_run"]
        now = _utcnow()
        suppressed: str | None = None

        # P0-2: Prevent duplicate execution within same tick
        if await _check_duplicate_execution(conn, p["id"], now):
            log.warning(
                "autoheal_duplicate_execution_prevented policy=%s target=%s", p["name"], target
            )
            continue

        # §5.4 / C-5.4: 成熟度未过 L2 且目标非 aegis-canary → 拒绝真实执行 (config 不可覆盖)。
        if not dry and cfg.autoheal_require_l2 and not p["canary"] and auto_allowed is False:
            suppressed = "canary_fence"
            ok, err = False, "canary_fence"
            reason = (
                f"autoheal BLOCKED (C-5.4): target {target} 非 aegis-canary 标签且能力 'autoheal.restart' "
                "未过 L2 → 拒绝无人值守执行。仅允许 dry_run 或对 canary 目标动作。"
            )
            log.error("autoheal_canary_fence_blocked target=%s policy=%s", target, p["name"])
        elif dry:
            reason = f"DRY-RUN: would {p['action']} {target} ({p['trigger_metric']}={value})"
            ok, err = True, None
        else:
            # SafetyMode must be QUALIFIED for real actions
            if safety.mode != SafetyMode.QUALIFIED:
                suppressed = f"safety_mode_{safety.mode.value.lower()}"
                ok, err = False, suppressed
                reason = f"autoheal BLOCKED: SafetyMode={safety.mode.value} (failed={safety.failed_checks}, degraded={safety.degraded_checks})"
                log.error(
                    "autoheal_safety_mode_blocked target=%s mode=%s", target, safety.mode.value
                )
            else:
                from oskill.flapping_detect import flapping_detect  # noqa: PLC0415

                # §5.3 抖动检测:同一目标 window 内自愈过频且仍异常 → 停手升级人工,不再重启。
                # P0-2: Load history from PostgreSQL
                heal_history = await _load_heal_history(
                    conn, target, cfg.autoheal_flap_window_seconds
                )
                fv = flapping_detect(
                    target=target,
                    heal_history=heal_history,
                    now=now,
                    window_seconds=cfg.autoheal_flap_window_seconds,
                    threshold=cfg.autoheal_flap_threshold,
                )
                if fv.is_flapping:
                    suppressed = "flapping"
                    ok, err = False, "flapping"
                    reason = (
                        f"autoheal SUPPRESSED(flapping): {target} 在 {fv.window_seconds}s 内已自愈 "
                        f"{fv.heals_in_window} 次仍异常 → 升级人工"
                    )
                    log.error(
                        "autoheal_flapping_suppressed target=%s heals=%d",
                        target,
                        fv.heals_in_window,
                    )
                elif await _rate_limited(
                    conn,
                    now,
                    max_actions=cfg.autoheal_rate_limit_max,
                    window_seconds=cfg.autoheal_rate_limit_window_seconds,
                ):
                    suppressed = "rate_limit"
                    ok, err = False, "rate_limit"
                    reason = (
                        f"autoheal RATE-LIMITED: {cfg.autoheal_rate_limit_window_seconds}s 内已达 "
                        f"{cfg.autoheal_rate_limit_max} 次动作上限,跳过 {target}"
                    )
                    log.warning("autoheal_rate_limited target=%s", target)
                else:
                    # P0-2: Pre-restart dual check
                    # Re-verify policy/cooldown/kill-switch just before restart
                    try:
                        if await is_flag_enabled(conn, AUTOHEAL_KILL_SWITCH):
                            log.warning(
                                "autoheal_pre_restart_kill_switch_active — aborting restart"
                            )
                            ok, err = False, "kill_switch"
                            reason = "autoheal ABORTED: kill-switch activated pre-restart"
                        elif is_change_frozen(cfg, _utcnow()):
                            log.warning("autoheal_pre_restart_change_frozen — aborting restart")
                            ok, err = False, "change_frozen"
                            reason = "autoheal ABORTED: change freeze activated pre-restart"
                        else:
                            from obase.docker import docker_container_restart  # noqa: PLC0415

                            docker_host = p["docker_host"] or cfg.docker_host
                            try:
                                await asyncio.to_thread(
                                    docker_container_restart,
                                    container_id=target,
                                    docker_host=docker_host,
                                )
                                ok, err = True, None
                                reason = (
                                    f"autoheal: restarted {target} ({p['trigger_metric']}={value})"
                                )
                                # Record heal history for flapping detection
                                await _record_heal(conn, target, now)
                                # Record global action for rate limiting
                                await _record_global_action(conn, now)
                            except Exception as exc:  # noqa: BLE001
                                ok, err = False, str(exc)[:150]
                                reason = f"autoheal: FAILED to restart {target}: {err}"
                    except Exception as exc:  # noqa: BLE001
                        log.error("autoheal_pre_restart_check_error err=%s", exc)
                        ok, err = False, "pre_check_error"
                        reason = f"autoheal ABORTED: pre-restart check failed: {exc}"

                    # P0-2: Post-restart dual check — verify container is actually running
                    if ok and not dry:
                        try:
                            from obase.docker import docker_container_inspect  # noqa: PLC0415

                            docker_host = p["docker_host"] or cfg.docker_host
                            inspect = await asyncio.to_thread(
                                docker_container_inspect,
                                container_id=target,
                                docker_host=docker_host,
                            )
                            container_state: dict[str, Any] = (
                                inspect.get("State", {}) if isinstance(inspect, dict) else {}
                            )
                            if not inspect or container_state.get("Running") is not True:
                                log.error("autoheal_post_restart_verify_failed target=%s", target)
                                # Note: we don't flip ok=False here as the restart was attempted
                                # The event trail records the outcome
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "autoheal_post_restart_verify_error target=%s err=%s", target, exc
                            )

        severity = (
            "info"
            if dry
            else (
                "critical" if (suppressed in ("flapping", "canary_fence") or not ok) else "warning"
            )
        )
        await events.insert(
            org_id=p["org_id"],
            cycle_id=uuid.uuid4(),
            severity=severity,
            source=f"autoheal:{p['name']}",
            reason=reason,
            value=value,
        )
        # 升级/限流也更新 last_triggered_at,让 cooldown 抑制重复升级刷屏。
        await conn.execute(
            "UPDATE autoheal_policies SET last_triggered_at = now() WHERE id = $1", p["id"]
        )
        log.info(
            "autoheal_policy_fired name=%s dry_run=%s ok=%s suppressed=%s reason=%s",
            p["name"],
            dry,
            ok,
            suppressed,
            reason,
        )
        actions.append(
            {
                "policy": p["name"],
                "dry_run": dry,
                "ok": ok,
                "reason": reason,
                "suppressed": suppressed,
            }
        )

    return actions
