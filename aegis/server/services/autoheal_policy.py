"""Policy-driven closed-loop autoheal (safe by design).

Each `autoheal_policies` row binds ONE target container to a trigger
(metric/operator/threshold) and an action (restart). The loop evaluates each
enabled policy against recent metrics for that container; on breach (and past the
per-policy cooldown) it either logs the intended action (dry_run, the default) or
executes it on that specific container. There is no blanket auto-remediation — a
real restart only happens for a policy explicitly set dry_run=false.

Outcomes are written to aegis_alert_events so they appear on the autoheal
dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository

log = logging.getLogger(__name__)

_LOOKBACK_SEC = 180

# §5.3 自愈安全层进程内状态(仅 loop-runner 实例跑自愈,advisory 锁保证单持有者 → 权威)。
# 目标→真实自愈时刻(抖动检测);全局真实动作时刻(限流)。重启进程即重置(可接受)。
_HEAL_HISTORY: dict[str, list[datetime]] = {}
_RECENT_ACTIONS: list[datetime] = []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limited(now: datetime, *, max_actions: int, window_seconds: int) -> bool:
    """全局真实自愈动作是否已达单位窗口上限。顺带剪枝过期时刻。"""
    cutoff = now.timestamp() - window_seconds
    _RECENT_ACTIONS[:] = [t for t in _RECENT_ACTIONS if t.timestamp() >= cutoff]
    return len(_RECENT_ACTIONS) >= max_actions


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
        return max(vals)
    if operator in ("<", "<="):
        return min(vals)
    return vals[0]


def _breached(value: float, operator: str, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
        "==": value == threshold,
    }.get(operator, False)


async def run_autoheal_policies(conn: asyncpg.Connection) -> list[dict]:
    """Evaluate all enabled policies; act on breaches past cooldown. Returns actions.

    §5.3 安全层(闸门顺序):全局急停(config + 运行时 flag)→ 抖动检测(同一目标自愈过频则
    停手升级人工)→ 全局限流(单位窗口动作上限)→ 才真实重启。dry_run 策略不受抖动/限流约束。"""
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415
    from aegis.server.services.platform_flags import (  # noqa: PLC0415
        AUTOHEAL_KILL_SWITCH,
        is_flag_enabled,
    )

    cfg = get_settings()
    # 全局急停:config 关 或 运行时 flag 置位 → 停止一切自愈(§5.3)。
    if not cfg.autoheal_enabled:
        log.info("autoheal_disabled_config — 跳过所有自愈")
        return []
    try:
        if await is_flag_enabled(conn, AUTOHEAL_KILL_SWITCH):
            log.warning("autoheal_kill_switch_active — 全局急停置位,跳过所有自愈动作")
            return []
    except Exception as exc:  # noqa: BLE001
        # 急停开关读取失败:保守放行(不因 flags 表故障瘫痪自愈),但记录。
        log.warning("autoheal_kill_switch_read_error err=%s (fail-open)", exc)

    # §9/§3.3 变更冻结窗口:高风险时段禁自动自愈(部署侧另有闸门)。
    from aegis.server.services.change_freeze import is_change_frozen  # noqa: PLC0415

    if is_change_frozen(cfg, _utcnow()):
        log.warning("autoheal_change_frozen — 变更冻结窗口内,禁止自动自愈(§9/§3.3)")
        return []

    policies = await conn.fetch(
        """
        SELECT id, org_id, name, target_container, trigger_metric, trigger_operator,
               trigger_threshold, action, dry_run, cooldown_seconds, docker_host,
               last_triggered_at
        FROM autoheal_policies
        WHERE enabled = TRUE
          AND (last_triggered_at IS NULL
               OR last_triggered_at <= now() - (cooldown_seconds * interval '1 second'))
        """
    )
    events = AutoHealEventRepository(conn)
    actions: list[dict] = []

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
        if dry:
            reason = f"DRY-RUN: would {p['action']} {target} ({p['trigger_metric']}={value})"
            ok, err = True, None
        else:
            from oskill.flapping_detect import flapping_detect  # noqa: PLC0415

            # §5.3 抖动检测:同一目标 window 内自愈过频且仍异常 → 停手升级人工,不再重启。
            fv = flapping_detect(
                target=target,
                heal_history=_HEAL_HISTORY.get(target, []),
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
                    "autoheal_flapping_suppressed target=%s heals=%d", target, fv.heals_in_window
                )
            elif _rate_limited(
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
                from obase.docker import docker_container_restart  # noqa: PLC0415

                docker_host = p["docker_host"] or cfg.docker_host
                try:
                    await asyncio.to_thread(
                        docker_container_restart, container_id=target, docker_host=docker_host
                    )
                    ok, err = True, None
                    reason = f"autoheal: restarted {target} ({p['trigger_metric']}={value})"
                    _HEAL_HISTORY.setdefault(target, []).append(now)  # 记入抖动历史
                    _RECENT_ACTIONS.append(now)  # 记入全局限流
                except Exception as exc:  # noqa: BLE001
                    ok, err = False, str(exc)[:150]
                    reason = f"autoheal: FAILED to restart {target}: {err}"

        severity = (
            "info" if dry else ("critical" if (suppressed == "flapping" or not ok) else "warning")
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
