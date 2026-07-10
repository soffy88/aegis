"""Tests for the stale-task reaper (devplatform Phase 1)."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.services import stale_task_reaper as r
from aegis.server.services.stale_task_reaper import (
    InvalidIdentifier,
    ReapResult,
    StaleTaskPolicy,
    _quote_ident,
    reap_on_connection,
    run_stale_task_reaper,
)

_ORG = uuid.uuid4()


def _policy(**over) -> StaleTaskPolicy:
    base = dict(
        id=uuid.uuid4(),
        org_id=_ORG,
        name="reap-mneme",
        target_dsn_secret=None,
        target_table="tasks",
        status_column="status",
        timestamp_column="updated_at",
        id_column=None,
        processing_value="processing",
        timeout_minutes=30,
        action="mark_failed",
        failed_value="failed",
        requeue_value="pending",
        max_actions_per_run=100,
        dry_run=True,
    )
    base.update(over)
    return StaleTaskPolicy(**base)


# ── identifier safety (SQL injection surface) ──────────────────────────────────


@pytest.mark.parametrize("ident", ["tasks", "public.tasks", "my_table", "s.t_1"])
def test_quote_ident_valid(ident: str) -> None:
    q = _quote_ident(ident)
    assert q.startswith('"') and q.count(".") == ident.count(".")


@pytest.mark.parametrize(
    "ident",
    ["tasks; DROP TABLE users", "tas ks", "a.b.c", "", "status = 1", 't"x', "a.b.c.d", "-x"],
)
def test_quote_ident_rejects_injection(ident: str) -> None:
    with pytest.raises(InvalidIdentifier):
        _quote_ident(ident)


# ── reap logic ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_dry_run_counts_without_updating() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 3
    res = await reap_on_connection(conn, _policy(dry_run=True))
    assert res.stuck_count == 3 and res.acted is False
    conn.execute.assert_not_awaited()  # dry_run never writes


@pytest.mark.asyncio
async def test_reap_marks_failed_when_not_dry_run() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 2
    res = await reap_on_connection(conn, _policy(dry_run=False, action="mark_failed"))
    assert res.stuck_count == 2 and res.acted is True
    sql, *args = conn.execute.await_args.args
    assert "UPDATE" in sql and "ctid IN" in sql  # bounded update
    assert args[0] == "failed"  # SET status = failed_value


@pytest.mark.asyncio
async def test_reap_requeue_uses_requeue_value() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 1
    await reap_on_connection(conn, _policy(dry_run=False, action="requeue", requeue_value="queued"))
    assert conn.execute.await_args.args[1:][0] == "queued"


@pytest.mark.asyncio
async def test_reap_with_id_column_returns_sample_ids() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"id": "a"}, {"id": "b"}]
    res = await reap_on_connection(conn, _policy(id_column="task_id", dry_run=True))
    assert res.stuck_count == 2 and res.sample_ids == ["a", "b"]


@pytest.mark.asyncio
async def test_reap_no_stuck_rows_is_noop() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 0
    res = await reap_on_connection(conn, _policy(dry_run=False))
    assert res.stuck_count == 0 and res.acted is False
    conn.execute.assert_not_awaited()


# ── cron entrypoint: one failing policy never blocks the others ────────────────


@pytest.mark.asyncio
async def test_run_isolates_per_policy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    good, bad = _policy(name="good"), _policy(name="bad")
    monkeypatch.setattr(r, "list_enabled_policies", mock.AsyncMock(return_value=[bad, good]))

    async def _reap(_conn, policy):
        if policy.name == "bad":
            raise RuntimeError("boom")
        return ReapResult(1, acted=False, action="mark_failed", sample_ids=None)

    monkeypatch.setattr(r, "reap_on_connection", _reap)
    recorded: list[tuple[str, ReapResult]] = []

    async def _record(_conn, policy, res):
        recorded.append((policy.name, res))

    monkeypatch.setattr(r, "_record", _record)

    results = await run_stale_task_reaper(mock.AsyncMock())

    assert len(results) == 2  # both processed despite one failing
    by_name = dict(recorded)
    assert by_name["bad"].error == "boom" and by_name["good"].stuck_count == 1


# ── router: identifier validation at create ────────────────────────────────────


def _client():
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.server.api.deps import get_db_conn
    from aegis.server.api.routers import stale_tasks
    from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

    app = FastAPI()
    app.include_router(stale_tasks.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(),
        email="o@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role="owner")],
    )
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"id": uuid.uuid4(), "org_id": _ORG, "name": "p"}

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False), conn


def _body(**over) -> dict:
    b = {
        "name": "reap",
        "target_table": "tasks",
        "status_column": "status",
        "timestamp_column": "updated_at",
        "processing_value": "processing",
    }
    b.update(over)
    return b


def test_create_rejects_unsafe_table_identifier() -> None:
    client, conn = _client()
    r = client.post(f"/api/v1/orgs/{_ORG}/stale-task-policies", json=_body(target_table="t; DROP"))
    assert r.status_code == 400
    conn.fetchrow.assert_not_called()  # never reached the INSERT


def test_create_happy_path() -> None:
    client, _ = _client()
    resp = client.post(f"/api/v1/orgs/{_ORG}/stale-task-policies", json=_body())
    assert resp.status_code == 201
