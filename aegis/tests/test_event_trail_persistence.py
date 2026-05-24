"""Tests for save_decision_trail persistence (4 tests per §4.3)."""

from __future__ import annotations

from unittest import mock

import pytest

from aegis.server.persistence.event_trail import save_decision_trail


def _mock_pool() -> mock.MagicMock:
    """Create a mock pool with acquire() context manager."""
    conn = mock.AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_save_decision_trail_completed() -> None:
    """Completed omodul run is persisted with severity=info."""
    pool = _mock_pool()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=pool):
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_abc123",
            decision_trail={"steps": [{"action": "pull_image"}]},
            user_id="user_1",
            status="completed",
        )

    conn = pool.acquire.return_value.__aenter__.return_value
    conn.execute.assert_called_once()
    sql = conn.execute.call_args[0][0]
    assert "INSERT INTO event_trail" in sql
    assert "ON CONFLICT (omodul_fingerprint) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_save_decision_trail_failed() -> None:
    """Failed omodul run is also persisted (severity=warning)."""
    pool = _mock_pool()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=pool):
        await save_decision_trail(
            omodul_name="triage_signal",
            fingerprint="fp_fail",
            decision_trail={"steps": []},
            user_id="user_2",
            status="failed",
            error={"msg": "timeout"},
        )

    conn = pool.acquire.return_value.__aenter__.return_value
    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    assert args[1] == "warning"  # severity param for failed


@pytest.mark.asyncio
async def test_idempotent_same_fingerprint() -> None:
    """Same fingerprint called twice → ON CONFLICT DO NOTHING (no error)."""
    pool = _mock_pool()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=pool):
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_dup",
            decision_trail={"steps": [{"action": "a"}]},
            user_id="user_1",
            status="completed",
        )
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_dup",
            decision_trail={"steps": [{"action": "a"}]},
            user_id="user_1",
            status="completed",
        )

    conn = pool.acquire.return_value.__aenter__.return_value
    assert conn.execute.call_count == 2


@pytest.mark.asyncio
async def test_decision_trail_contains_steps() -> None:
    """The JSONB payload contains the full decision_trail steps."""
    import json

    pool = _mock_pool()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=pool):
        trail = {"steps": [{"action": "pull"}, {"action": "start"}, {"action": "health_check"}]}
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_steps",
            decision_trail=trail,
            user_id="user_1",
            status="completed",
        )

    conn = pool.acquire.return_value.__aenter__.return_value
    payload_json = conn.execute.call_args[0][2]
    payload = json.loads(payload_json)
    assert payload["decision_trail"] == trail
    assert len(payload["decision_trail"]["steps"]) == 3


@pytest.mark.asyncio
async def test_failed_then_completed_retry_sql_uses_conflict_target() -> None:
    """MF3: failed first, then completed retry — both issue INSERT with ON CONFLICT (omodul_fingerprint).

    ADR-002 方案 A: DB keeps first record (failed), completed retry is silently dropped.
    """
    import json

    pool = _mock_pool()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=pool):
        # First call: failed
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_retry_001",
            decision_trail={"steps": []},
            user_id="user_a",
            status="failed",
            error={"error_class": "ConnectionError", "error_message": "docker down"},
        )
        # Second call: completed (retry)
        await save_decision_trail(
            omodul_name="install_self_hosted_app",
            fingerprint="fp_retry_001",
            decision_trail={"steps": [{"step_no": 1}]},
            user_id="user_a",
            status="completed",
        )

    conn = pool.acquire.return_value.__aenter__.return_value
    assert conn.execute.call_count == 2

    # Both use ON CONFLICT (omodul_fingerprint) DO NOTHING
    for call in conn.execute.call_args_list:
        sql = call[0][0]
        assert "ON CONFLICT (omodul_fingerprint) DO NOTHING" in sql

    # First call has severity=warning (failed), second has severity=info (completed)
    first_severity = conn.execute.call_args_list[0][0][1]
    second_severity = conn.execute.call_args_list[1][0][1]
    assert first_severity == "warning"
    assert second_severity == "info"

    # First call payload contains error
    first_payload = json.loads(conn.execute.call_args_list[0][0][2])
    assert first_payload["status"] == "failed"
    assert first_payload["error"]["error_class"] == "ConnectionError"
