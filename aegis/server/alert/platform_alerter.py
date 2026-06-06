"""Platform Alerter — AlerterEngine assembly for Aegis platform monitoring.

S1 bypass: AlerterEngine is instantiated directly (not via assemble()) because
oservice's assembler rejects callables whose __module__ isn't an 3O package.
Fix tracked in oservice v0.4.2 (triage.injection_points PATCH SPEC).

AEGIS_DESIGN v1.1.0 §6.1 + AEGIS_3O_IMPL_SPEC v1.0 §1.5.
Runs parallel to aegis/server/engines/alert_engine.py (C2-2 Sentry-style aggregator).

TODO(AEGIS-BACKLOG-070): bypass oservice.assemble(manifest), 直接 new AlerterEngine.
根因: oservice v0.4.1 装配器 _detect_element_kind 通过 callable.__module__ 判 kind,
Aegis 内部 wrapper 函数 module=aegis.*, 不通过 kind='oprim' 校验.
Owner backlog: oservice v0.4.2 修 _detect_element_kind 后, 切回 assemble(manifest) 模式.
同时见: AEGIS-BACKLOG-071 (evaluator wrapper 必要根因 = oprim 真签名与
AlerterEngine evaluator 协议不一致).
"""

from __future__ import annotations

import logging
from typing import Any

from obase.notify import TelegramRequest, telegram_send
from oprim import (
    fs_disk_usage,
    postgres_long_running_queries,
    postgres_pool_status,
    rabbitmq_consumer_count,
    rabbitmq_queue_depth,
    system_cpu_usage,
    system_ram_usage,
)
from oservice.engines.alerter import AlerterEngine

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

# ── Evaluator wrappers ────────────────────────────────────────────────────────
# Protocol (AlerterEngine._call_evaluator): evaluator(config=dict) → list[dict]
# Each dict must contain: entity_id, severity, message


def postgres_pool_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    dsn = config.get("dsn", "")
    if not dsn:
        return []
    try:
        result = postgres_pool_status(dsn=dsn, timeout_sec=config.get("timeout_sec", 5))
        threshold = config.get("pool_usage_threshold", 85.0)
        if result.usage_percent < threshold:
            return []
        sev = "critical" if result.usage_percent >= 95.0 else "warning"
        return [
            {
                "entity_id": "postgres_pool",
                "severity": sev,
                "message": f"pool usage {result.usage_percent:.1f}% >= {threshold:.1f}%",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "postgres_pool", "severity": "critical", "message": str(exc)}]


def postgres_slow_queries_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    dsn = config.get("dsn", "")
    if not dsn:
        return []
    try:
        threshold_ms = config.get("threshold_ms", 5000)
        queries = postgres_long_running_queries(
            dsn=dsn,
            threshold_ms=threshold_ms,
            timeout_sec=config.get("timeout_sec", 5),
        )
        if not queries:
            return []
        return [
            {
                "entity_id": "postgres_slow_query",
                "severity": "warning",
                "message": f"{len(queries)} queries running > {threshold_ms}ms",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "postgres_slow_query", "severity": "critical", "message": str(exc)}]


def rabbitmq_queue_depth_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    mgmt_url = config.get("mgmt_url", "")
    if not mgmt_url:
        return []
    try:
        queue_name = config.get("queue_name", "tasks")
        depth = rabbitmq_queue_depth(mgmt_url=mgmt_url, queue_name=queue_name)
        threshold = config.get("threshold", 1000)
        if depth < threshold:
            return []
        return [
            {
                "entity_id": f"rabbitmq_queue_{queue_name}",
                "severity": "warning",
                "message": f"queue depth {depth} >= {threshold}",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "rabbitmq_queue_depth", "severity": "critical", "message": str(exc)}]


def rabbitmq_consumer_count_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    mgmt_url = config.get("mgmt_url", "")
    if not mgmt_url:
        return []
    try:
        queue_name = config.get("queue_name", "tasks")
        count = rabbitmq_consumer_count(mgmt_url=mgmt_url, queue_name=queue_name)
        min_consumers = config.get("min_consumers", 1)
        if count >= min_consumers:
            return []
        return [
            {
                "entity_id": f"rabbitmq_consumers_{queue_name}",
                "severity": "critical",
                "message": f"consumer count {count} < minimum {min_consumers}",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "rabbitmq_consumers", "severity": "critical", "message": str(exc)}]


def system_cpu_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        usage = system_cpu_usage()
        threshold = config.get("threshold", 85.0)
        if usage < threshold:
            return []
        sev = "critical" if usage >= 95.0 else "warning"
        return [
            {
                "entity_id": "system_cpu",
                "severity": sev,
                "message": f"CPU {usage:.1f}% >= {threshold:.1f}%",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "system_cpu", "severity": "critical", "message": str(exc)}]


def system_ram_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        stats = system_ram_usage()
        used_pct = stats.get("used_percent", 0.0)
        threshold = config.get("threshold", 90.0)
        if used_pct < threshold:
            return []
        sev = "critical" if used_pct >= 95.0 else "warning"
        return [
            {
                "entity_id": "system_ram",
                "severity": sev,
                "message": f"RAM {used_pct:.1f}% >= {threshold:.1f}%",
            }
        ]
    except Exception as exc:
        return [{"entity_id": "system_ram", "severity": "critical", "message": str(exc)}]


def disk_usage_evaluator(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    path = config.get("path", "/")
    try:
        result = fs_disk_usage(path=path)
        threshold = config.get("threshold", 85.0)
        if result.used_percent < threshold:
            return []
        sev = "critical" if result.used_percent >= 95.0 else "warning"
        return [
            {
                "entity_id": f"disk_{path}",
                "severity": sev,
                "message": f"disk {result.used_percent:.1f}% >= {threshold:.1f}%",
            }
        ]
    except Exception as exc:
        return [{"entity_id": f"disk_{path}", "severity": "critical", "message": str(exc)}]


# ── Channel wrapper ───────────────────────────────────────────────────────────
# Protocol (AlerterEngine._build_channel_payload): channel(text=, chat_id=, bot_token=)
# obase.notify.telegram_send takes TelegramRequest — this adapter bridges the gap.


def telegram_channel(*, text: str, chat_id: str, bot_token: str, **_: Any) -> None:
    if not bot_token:
        log.warning(
            "telegram_channel skipped: bot_token empty (alert text: %s, chat_id: %s)",
            text[:100],
            chat_id,
        )
        return
    telegram_send(TelegramRequest(text=text, chat_id=chat_id, bot_token=bot_token))


# ── Assembly ──────────────────────────────────────────────────────────────────


def build_platform_alerter(cfg: AegisSettings) -> AlerterEngine:
    thresholds: dict[str, Any] = cfg.platform_alerter_thresholds
    return AlerterEngine(
        evaluators=[
            postgres_pool_evaluator,
            postgres_slow_queries_evaluator,
            rabbitmq_queue_depth_evaluator,
            rabbitmq_consumer_count_evaluator,
            system_cpu_evaluator,
            system_ram_evaluator,
            disk_usage_evaluator,
        ],
        channels=[telegram_channel],
        trigger={"on_interval": cfg.platform_alerter_interval_seconds},
        config={
            "throttle_seconds": cfg.platform_alerter_throttle_seconds,
            "evaluator_configs": {
                "postgres_pool_evaluator": {
                    "dsn": cfg.postgres_dsn,
                    "pool_usage_threshold": thresholds.get("pool_usage_percent", 85.0),
                },
                "postgres_slow_queries_evaluator": {
                    "dsn": cfg.postgres_dsn,
                    "threshold_ms": thresholds.get("slow_query_ms", 5000),
                },
                "rabbitmq_queue_depth_evaluator": {
                    "mgmt_url": cfg.platform_alerter_rabbitmq_mgmt_url,
                    "queue_name": cfg.platform_alerter_rabbitmq_queue_name,
                    "threshold": thresholds.get("rabbitmq_queue_depth", 1000),
                },
                "rabbitmq_consumer_count_evaluator": {
                    "mgmt_url": cfg.platform_alerter_rabbitmq_mgmt_url,
                    "queue_name": cfg.platform_alerter_rabbitmq_queue_name,
                    "min_consumers": thresholds.get("rabbitmq_consumer_min", 1),
                },
                "system_cpu_evaluator": {
                    "threshold": thresholds.get("cpu_percent", 85.0),
                },
                "system_ram_evaluator": {
                    "threshold": thresholds.get("ram_percent", 90.0),
                },
                "disk_usage_evaluator": {
                    "path": cfg.platform_alerter_disk_path,
                    "threshold": thresholds.get("disk_percent", 85.0),
                },
            },
            "channel_configs": {
                "telegram_channel": {
                    "bot_token": cfg.platform_alerter_telegram_bot_token,
                    "chat_id": cfg.platform_alerter_telegram_chat_id,
                },
            },
        },
        name="aegis-platform-alerter",
    )


# ── Module-level singleton ────────────────────────────────────────────────────

_platform_alerter_service: AlerterEngine | None = None


def get_platform_alerter() -> AlerterEngine | None:
    return _platform_alerter_service


def init_platform_alerter(cfg: AegisSettings) -> AlerterEngine:
    global _platform_alerter_service
    _platform_alerter_service = build_platform_alerter(cfg)
    return _platform_alerter_service
