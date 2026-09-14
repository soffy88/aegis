"""SafetyMode — unified production qualification gate.

Computes QUALIFIED | DEGRADED | BLOCKED at startup and exposes it via
GET /api/v1/health/qualification.

All mutation endpoints and background actuators MUST call the same safety gate.
No module decides fail-open/fail-closed independently.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

import asyncpg

from aegis.server.runtime.config import get_settings
from aegis.server.services.platform_flags import AUTOHEAL_KILL_SWITCH, is_flag_enabled

log = logging.getLogger(__name__)


class SafetyMode(StrEnum):
    QUALIFIED = "QUALIFIED"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str | None = None


@dataclass
class SafetyQualification:
    mode: SafetyMode
    failed_checks: list[str]
    degraded_checks: list[str]
    details: list[CheckResult]


async def _check_postgres(conn: asyncpg.Connection) -> CheckResult:
    try:
        await conn.execute("SELECT 1")
        return CheckResult(name="postgres", passed=True)
    except Exception as exc:
        return CheckResult(name="postgres", passed=False, message=str(exc))


async def _check_redis() -> CheckResult:
    try:
        import redis.asyncio as redis

        settings = get_settings()
        client = redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        await client.ping()
        await client.aclose()
        return CheckResult(name="redis", passed=True)
    except Exception as exc:
        return CheckResult(name="redis", passed=False, message=str(exc))


async def _check_docker() -> CheckResult:
    try:
        import docker

        client = docker.from_env(timeout=3)
        client.ping()
        return CheckResult(name="docker", passed=True)
    except Exception as exc:
        return CheckResult(name="docker", passed=False, message=str(exc))


async def _check_secrets_master_key(conn: asyncpg.Connection) -> CheckResult:
    """secrets master key must be independently configured (not derived from jwt_secret)."""
    settings = get_settings()
    if settings.secrets_master_key and "CHANGE-IN-PROD" not in settings.secrets_master_key:
        return CheckResult(name="secrets_master_key", passed=True)
    return CheckResult(
        name="secrets_master_key",
        passed=False,
        message="AEGIS_SECRETS_MASTER_KEY not set or uses placeholder",
    )


async def _check_prod_registration_closed() -> CheckResult:
    """prod registration must be disabled."""
    settings = get_settings()
    if settings.env == "prod":
        # Check env var; default False for prod
        import os

        reg_enabled = os.environ.get("AEGIS_REGISTRATION_ENABLED", "false").lower() == "true"
        if not reg_enabled:
            return CheckResult(name="prod_registration_closed", passed=True)
        return CheckResult(
            name="prod_registration_closed",
            passed=False,
            message="AEGIS_REGISTRATION_ENABLED=true in prod",
        )
    return CheckResult(name="prod_registration_closed", passed=True)


async def _check_external_deadman_configured() -> CheckResult:
    """External deadman heartbeat URL must be configured in prod."""
    settings = get_settings()
    if settings.env == "prod":
        if settings.deadman_heartbeat_url:
            return CheckResult(name="external_deadman", passed=True)
        return CheckResult(
            name="external_deadman",
            passed=False,
            message="AEGIS_DEADMAN_HEARTBEAT_URL not set in prod",
        )
    return CheckResult(name="external_deadman", passed=True)


async def _check_retention_storage_guard(conn: asyncpg.Connection) -> CheckResult:
    """Retention/storage guard must be enabled (retention policies present)."""
    try:
        from aegis.server.persistence.retention import RETENTION  # noqa: PLC0415

        if RETENTION:
            return CheckResult(name="retention_storage_guard", passed=True)
        return CheckResult(
            name="retention_storage_guard",
            passed=False,
            message="No retention policies configured",
        )
    except Exception as exc:
        return CheckResult(name="retention_storage_guard", passed=False, message=str(exc))


async def _check_self_backup_configured(conn: asyncpg.Connection) -> CheckResult:
    """Self-backup must be configured and have at least one completed backup."""
    settings = get_settings()
    if settings.self_backup_interval_hours <= 0:
        return CheckResult(
            name="self_backup", passed=False, message="self_backup_interval_hours <= 0"
        )
    # Check if at least one backup completed
    row = await conn.fetchrow("SELECT 1 FROM aegis_backups WHERE status = 'completed' LIMIT 1")
    if row:
        return CheckResult(name="self_backup", passed=True)
    return CheckResult(
        name="self_backup",
        passed=False,
        message="No completed self-backup found",
    )


async def _check_autoheal_kill_switch_readable(conn: asyncpg.Connection) -> CheckResult:
    """AutoHeal kill-switch must be readable (platform_flags table accessible)."""
    try:
        await is_flag_enabled(conn, AUTOHEAL_KILL_SWITCH)
        return CheckResult(name="autoheal_kill_switch", passed=True)
    except Exception as exc:
        return CheckResult(name="autoheal_kill_switch", passed=False, message=str(exc))


async def compute_safety_mode(conn: asyncpg.Connection) -> SafetyQualification:
    """Compute the unified SafetyMode.

    QUALIFIED: All critical checks pass → R1/R2 auto actions allowed.
    DEGRADED: Non-critical checks fail → Monitor/Analyze only, no mutations.
    BLOCKED: Critical dependency failed → No mutations at all.
    """
    # Critical checks (BLOCKED if any fail)
    critical_checks = [
        _check_postgres(conn),
        _check_redis(),
        _check_docker(),
    ]

    # Degraded checks (DEGRADED if any fail, but not BLOCKED)
    degraded_checks = [
        _check_secrets_master_key(conn),
        _check_prod_registration_closed(),
        _check_external_deadman_configured(),
        _check_retention_storage_guard(conn),
        _check_self_backup_configured(conn),
        _check_autoheal_kill_switch_readable(conn),
    ]

    critical_results = await asyncio.gather(*critical_checks)
    degraded_results = await asyncio.gather(*degraded_checks)

    all_results = list(critical_results) + list(degraded_results)

    failed = [r.name for r in all_results if not r.passed]
    critical_failed = [r.name for r in critical_results if not r.passed]
    degraded_failed = [r.name for r in degraded_results if not r.passed]

    if critical_failed:
        mode = SafetyMode.BLOCKED
    elif degraded_failed:
        mode = SafetyMode.DEGRADED
    else:
        mode = SafetyMode.QUALIFIED

    log.info(
        "safety_mode_computed mode=%s failed=%s critical_failed=%s",
        mode.value,
        failed,
        critical_failed,
    )

    return SafetyQualification(
        mode=mode,
        failed_checks=critical_failed,
        degraded_checks=degraded_failed,
        details=all_results,
    )


async def assert_safety_allows_mutation(conn: asyncpg.Connection, *, action: str) -> None:
    """Unified safety gate for all mutation endpoints and actuators.

    Raises if SafetyMode doesn't allow the requested action.
    """
    qual = await compute_safety_mode(conn)

    if qual.mode == SafetyMode.BLOCKED:
        from aegis.server.exceptions import AegisError  # noqa: PLC0415

        raise AegisError(
            f"safety_mode=BLOCKED: mutation '{action}' blocked — critical dependencies failed: "
            f"{qual.failed_checks}. Fix infrastructure before attempting mutations."
        )

    if qual.mode == SafetyMode.DEGRADED:
        from aegis.server.exceptions import AegisError  # noqa: PLC0415

        raise AegisError(
            f"safety_mode=DEGRADED: mutation '{action}' blocked — degraded checks failed: "
            f"{qual.degraded_checks}. Only monitor/analyze actions allowed."
        )


__all__ = [
    "SafetyMode",
    "SafetyQualification",
    "CheckResult",
    "compute_safety_mode",
    "assert_safety_allows_mutation",
]
