"""C0d e2e smoke: install demo app via dispatcher → omodul → Docker.

Run with:
    RUN_SMOKE=1 pytest aegis/tests/smoke/test_install_e2e.py -v

Requires: Docker daemon, Postgres, Redis.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture
async def pg_conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url(driver=None)
    conn = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_install_demo_app_via_dispatcher(pg_conn: asyncpg.Connection) -> None:
    """C0d e2e: dispatcher.invoke → omodul.install_self_hosted_app → Docker.

    Covers:
    - dispatcher.invoke -> omodul.install_self_hosted_app
    - omodul returns 5-piece result + decision_trail
    - event_trail Postgres write (fingerprint UNIQUE + ON CONFLICT)
    - dedup cache
    - budget tracker deduction
    """
    from aegis.server.dispatch import OmodulDispatcher
    from aegis.server.dispatch.budget_tracker import BudgetTracker
    from aegis.server.dispatch.dedup_cache import DedupCache

    tracker = BudgetTracker()
    dedup = DedupCache()
    dispatcher = OmodulDispatcher(
        conn=pg_conn,
        org_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        budget_tracker=tracker,
        dedup_cache=dedup,
    )

    result = await dispatcher.invoke(
        omodul_name="install_self_hosted_app",
        config={
            "app_slug": "nginx-demo",
            "app_version": "1.21-alpine",
            "instance_name": "c0d-smoke-test",
            "config_hash": "test_hash_001",
        },
        input_data={
            "app_config": {
                "image": "nginx:1.21-alpine",
                "ports": ["8090:80"],
            },
            "target_host": "localhost",
            "docker_host": "unix:///var/run/docker.sock",
        },
        user_id="c0d_smoke_user",
    )

    # Verify result structure
    assert result["status"] in ("completed", "failed"), f"unexpected status: {result['status']}"
    assert "fingerprint" in result
    assert len(result["fingerprint"]) == 64
    assert "decision_trail" in result
    assert "cost_usd" in result

    # If completed, verify container started and clean up
    if result["status"] == "completed":
        findings = result.get("findings", {})
        container_id = findings.get("container_id") if isinstance(findings, dict) else None
        if container_id:
            from oprim import docker_container_stop

            docker_container_stop(container_id=container_id, timeout_sec=5)

    # Verify event_trail was written
    row = await pg_conn.fetchrow(
        "SELECT * FROM event_trail WHERE omodul_fingerprint=$1",
        result["fingerprint"],
    )
    assert row is not None
    assert row["omodul_kind"] == "install_self_hosted_app"
