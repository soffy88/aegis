"""Stale-task reaper (devplatform Phase 1).

The "PG status column + async worker" pattern loses tasks when a worker is hard-killed:
the row stays in its in-progress status forever (no timeout, no dead-letter). This is a
cross-project ops job — any project (Mneme/Tide/Helivex/…) that declares a policy gets
its stuck rows reaped on a schedule, with a decision_trail.

Reuses: the autoheal_policies cron/policy/dry_run pattern, save_decision_trail (the
decision_trail four-要素 element), and the secrets vault (external-DB DSN resolution).

Safety:
- identifiers (table/column names) come from the policy and cannot be parameterized, so
  they are strictly validated + double-quoted; all *values* are bound parameters.
- dry_run defaults TRUE (observe first); max_actions_per_run caps rows touched per tick
  (circuit-breaker against runaway requeue storms); the UPDATE re-checks the predicate.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg

from aegis.server.persistence.event_trail import save_decision_trail
from aegis.server.services.secrets_vault import reveal_secret

log = logging.getLogger(__name__)

# system principal for decision_trail rows written by the reaper cron (no user context)
_SYSTEM_PRINCIPAL = "00000000-0000-0000-0000-0000000000re"

_IDENT_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class InvalidIdentifier(ValueError):
    """A policy-declared table/column name is not a safe SQL identifier."""


def _quote_ident(name: str) -> str:
    """Validate + double-quote a (optionally schema-qualified) identifier. Rejects
    anything that isn't a bare identifier so it can be safely interpolated into SQL."""
    parts = name.split(".")
    if not (1 <= len(parts) <= 2) or not all(_IDENT_PART.match(p) for p in parts):
        raise InvalidIdentifier(f"unsafe SQL identifier: {name!r}")
    return ".".join(f'"{p}"' for p in parts)


@dataclass
class StaleTaskPolicy:
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    target_dsn_secret: str | None
    target_table: str
    status_column: str
    timestamp_column: str
    id_column: str | None
    processing_value: str
    timeout_minutes: int
    action: str
    failed_value: str
    requeue_value: str
    max_actions_per_run: int
    dry_run: bool

    @classmethod
    def from_row(cls, r: asyncpg.Record) -> StaleTaskPolicy:
        return cls(**{k: r[k] for k in cls.__dataclass_fields__})


@dataclass
class ReapResult:
    stuck_count: int
    acted: bool  # False when dry_run
    action: str
    sample_ids: list[str] | None
    error: str | None = None


async def list_enabled_policies(conn: asyncpg.Connection) -> list[StaleTaskPolicy]:
    rows = await conn.fetch(
        "SELECT * FROM stale_task_policies WHERE enabled = TRUE ORDER BY created_at"
    )
    return [StaleTaskPolicy.from_row(r) for r in rows]


@contextlib.asynccontextmanager
async def _target_connection(
    aegis_conn: asyncpg.Connection, policy: StaleTaskPolicy
) -> AsyncIterator[asyncpg.Connection]:
    """Yield a connection to the policy's target DB. Same-DB (Aegis) when no DSN secret
    is set; otherwise resolve the DSN from the secrets vault and open a scoped connection."""
    if not policy.target_dsn_secret:
        yield aegis_conn
        return
    dsn = await reveal_secret(aegis_conn, org_id=policy.org_id, name=policy.target_dsn_secret)
    if not dsn:
        raise ValueError(f"target_dsn_secret {policy.target_dsn_secret!r} not found in vault")
    ext = await asyncpg.connect(dsn)
    try:
        yield ext
    finally:
        await ext.close()


async def reap_on_connection(conn: asyncpg.Connection, policy: StaleTaskPolicy) -> ReapResult:
    """Find + (unless dry_run) reap stuck rows on the given connection. Pure per-policy
    logic — the connection may be Aegis's own or a resolved external target DB."""
    tbl = _quote_ident(policy.target_table)
    statcol = _quote_ident(policy.status_column)
    tscol = _quote_ident(policy.timestamp_column)
    # timeout_minutes / max_actions_per_run are validated ints (CHECK + Pydantic bounds)
    # → safe to interpolate as literals, which sidesteps asyncpg's indeterminate parameter
    # types in LIMIT / interval. Only the string *values* are bound parameters.
    timeout = int(policy.timeout_minutes)
    limit = int(policy.max_actions_per_run)

    def _predicate(pval_ph: str) -> str:
        # stuck = still in the processing status AND older than the timeout. status cast
        # to text so it matches enum/text/int status columns uniformly.
        return f"{statcol}::text = {pval_ph} AND {tscol} < now() - interval '{timeout} minutes'"

    sample_ids: list[str] | None = None
    if policy.id_column:
        idcol = _quote_ident(policy.id_column)
        rows = await conn.fetch(
            f"SELECT {idcol}::text AS id FROM {tbl} WHERE {_predicate('$1')} LIMIT {limit}",
            policy.processing_value,
        )
        sample_ids = [r["id"] for r in rows]
        stuck = len(sample_ids)
    else:
        stuck = (
            await conn.fetchval(
                f"SELECT count(*) FROM (SELECT 1 FROM {tbl} "
                f"WHERE {_predicate('$1')} LIMIT {limit}) s",
                policy.processing_value,
            )
            or 0
        )

    if policy.dry_run or stuck == 0:
        return ReapResult(stuck, acted=False, action=policy.action, sample_ids=sample_ids)

    new_value = policy.failed_value if policy.action == "mark_failed" else policy.requeue_value
    # bounded, predicate-rechecking UPDATE ($1=new value, $2=processing value)
    await conn.execute(
        f"UPDATE {tbl} SET {statcol} = $1 "
        f"WHERE ctid IN (SELECT ctid FROM {tbl} WHERE {_predicate('$2')} LIMIT {limit})",
        new_value,
        policy.processing_value,
    )
    return ReapResult(stuck, acted=True, action=policy.action, sample_ids=sample_ids)


def _fingerprint(policy: StaleTaskPolicy, result: ReapResult) -> str:
    raw = f"stale_task_reaper:{policy.id}:{result.stuck_count}:{result.action}:{result.acted}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _record(aegis_conn: asyncpg.Connection, policy: StaleTaskPolicy, r: ReapResult) -> None:
    """Persist the reap event + a decision_trail (four-要素) + refresh last_run_at."""
    detail: dict[str, Any] = {
        "policy": policy.name,
        "target_table": policy.target_table,
        "action": r.action,
        "dry_run": policy.dry_run,
        "stuck_count": r.stuck_count,
        "timeout_minutes": policy.timeout_minutes,
    }
    if r.sample_ids is not None:
        detail["sample_ids"] = r.sample_ids[:50]
    if r.error:
        detail["error"] = r.error

    await aegis_conn.execute(
        "INSERT INTO stale_task_reap_events (policy_id, org_id, stuck_count, action, dry_run, detail)"
        " VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
        policy.id,
        policy.org_id,
        r.stuck_count,
        r.action,
        policy.dry_run,
        json.dumps(detail),
    )
    await aegis_conn.execute(
        "UPDATE stale_task_policies SET last_run_at = now() WHERE id = $1", policy.id
    )
    # decision_trail four-要素 element (reuses the omodul trail writer; idempotent by fp)
    with contextlib.suppress(Exception):
        await save_decision_trail(
            omodul_name="stale_task_reaper",
            fingerprint=_fingerprint(policy, r),
            decision_trail=detail,
            user_id=_SYSTEM_PRINCIPAL,
            status="error" if r.error else "completed",
            error={"message": r.error} if r.error else None,
        )


async def run_stale_task_reaper(aegis_conn: asyncpg.Connection) -> list[ReapResult]:
    """Cron entrypoint: reap every enabled policy. One failing policy never blocks the
    others; its error is recorded and it moves on."""
    results: list[ReapResult] = []
    for policy in await list_enabled_policies(aegis_conn):
        try:
            async with _target_connection(aegis_conn, policy) as target:
                r = await reap_on_connection(target, policy)
        except Exception as exc:  # noqa: BLE001 — isolate per-policy failures
            log.warning("stale_task_reaper policy=%s failed: %s", policy.name, exc)
            r = ReapResult(0, acted=False, action=policy.action, sample_ids=None, error=str(exc))
        await _record(aegis_conn, policy, r)
        if r.stuck_count:
            log.info(
                "stale_task_reaper policy=%s stuck=%d acted=%s dry_run=%s",
                policy.name,
                r.stuck_count,
                r.acted,
                policy.dry_run,
            )
        results.append(r)
    return results
