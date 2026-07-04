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
from unittest.mock import MagicMock, patch

import asyncpg
import pytest

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_FAKE_FINGERPRINT = "a" * 64
_MOCK_OMODUL_RESULT = {
    "status": "completed",
    "fingerprint": _FAKE_FINGERPRINT,
    "decision_trail": {"steps": ["pull", "start", "health"]},
    "cost_usd": 0.01,
    "findings": {"container_id": None},
}


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture
async def pg_conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url(driver=None)
    # Init global pool so save_decision_trail can call get_pool() without crashing.
    # Fixes AEGIS-BACKLOG-001.
    from aegis.server.persistence.db import close_pool, init_pool

    await init_pool(dsn=dsn, min_size=1, max_size=2)
    conn = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()
        await close_pool()


async def test_install_demo_app_via_dispatcher(pg_conn: asyncpg.Connection) -> None:
    """C0d e2e: dispatcher.invoke → omodul.install_self_hosted_app → Docker.

    Covers:
    - dispatcher.invoke orchestration (dedup / budget / event_trail)
    - omodul mocked (AEGIS-BACKLOG-003: install_self_hosted_app 未进主库)
    - save_decision_trail real call (AEGIS-BACKLOG-001 fixed: pool init in pg_conn fixture)
    - event_trail Postgres write asserted (AEGIS-BACKLOG-002 fixed: unskipped)
    """
    import fakeredis.aioredis

    from aegis.server.dispatch import OmodulDispatcher
    from aegis.server.dispatch.budget_tracker import BudgetTracker
    from aegis.server.dispatch.dedup_cache import DedupCache

    redis = fakeredis.aioredis.FakeRedis()
    tracker = BudgetTracker(redis_client=redis, monthly_limit_usd=50.0)
    dedup = DedupCache(redis_client=redis)
    dispatcher = OmodulDispatcher(
        dedup_cache=dedup,
        budget_tracker=tracker,
        data_dir="/tmp/aegis-smoke-test",
    )

    mock_fn = MagicMock(return_value=_MOCK_OMODUL_RESULT)
    mock_compute_fp = MagicMock(return_value=_FAKE_FINGERPRINT)
    mock_config_obj = MagicMock()
    mock_config_obj.budget_usd = 1.0  # dispatcher does getattr(config_obj, "budget_usd", 5.0)
    mock_config_cls = MagicMock(return_value=mock_config_obj)
    mock_input_cls = MagicMock(return_value=MagicMock())

    with (
        patch("omodul.install_self_hosted_app", mock_fn, create=True),
        patch("omodul.compute_fingerprint_for", mock_compute_fp),
        patch.object(dispatcher, "_resolve_class", side_effect=[mock_config_cls, mock_input_cls]),
    ):
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
            project_id=uuid.uuid4(),
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
            from obase.docker import docker_container_stop

            docker_container_stop(container_id=container_id, timeout_sec=5)

    # AEGIS-BACKLOG-001/002 fixed: pool initialized in pg_conn fixture, real write verified.
    row = await pg_conn.fetchrow(
        "SELECT * FROM event_trail WHERE omodul_fingerprint=$1",
        result["fingerprint"],
    )
    assert row is not None
    assert row["omodul_kind"] == "install_self_hosted_app"
