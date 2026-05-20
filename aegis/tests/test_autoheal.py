"""Tests for AutoHealDispatcher skeleton."""
from __future__ import annotations

import uuid
from typing import Any
from unittest import mock

import pytest

from aegis.server.orchestration.autoheal import AutoHealDispatcher


def _fake_plugin(name: str, pattern: str) -> Any:
    cls = mock.MagicMock()
    cls.name = name
    cls.matches_alert = pattern
    cls.__name__ = f"FakePlugin_{name}"
    return cls


class TestAutoHealDispatcher:
    def test_no_match(self) -> None:
        d = AutoHealDispatcher(plugins={}, dry_run=True)
        result = d.find_matching_plugins("rabbitmq.connection_reset")
        assert result == []

    def test_substring_match(self) -> None:
        p1 = _fake_plugin("a", "rabbitmq")
        p2 = _fake_plugin("b", "postgres")
        d = AutoHealDispatcher(plugins={"a": p1, "b": p2}, dry_run=True)
        result = d.find_matching_plugins("rabbitmq.connection_reset")
        assert len(result) == 1
        assert result[0].name == "a"

    @pytest.mark.asyncio
    async def test_dispatch_dry_run(
        self,
        mock_db_conn: mock.AsyncMock,
        test_org_id: uuid.UUID,
        test_project_id: uuid.UUID,
    ) -> None:
        mock_db_conn.fetchrow.return_value = {"id": str(uuid.uuid4())}
        p = _fake_plugin("rabbitmq-reset", "rabbitmq")
        d = AutoHealDispatcher(plugins={"rabbitmq-reset": p}, dry_run=True)

        result = await d.dispatch(
            conn=mock_db_conn,
            org_id=test_org_id, project_id=test_project_id,
            alert_payload={"alert_name": "rabbitmq.foo"},
            trace_id="trc",
        )
        assert "rabbitmq-reset" in result["matched"]
        assert result["results"][0]["outcome"] == "dry_run"

    @pytest.mark.asyncio
    async def test_dispatch_no_matches(
        self,
        mock_db_conn: mock.AsyncMock,
        test_org_id: uuid.UUID,
        test_project_id: uuid.UUID,
    ) -> None:
        d = AutoHealDispatcher(plugins={}, dry_run=True)
        result = await d.dispatch(
            conn=mock_db_conn,
            org_id=test_org_id, project_id=test_project_id,
            alert_payload={"alert_name": "x"},
            trace_id="trc",
        )
        assert result["outcome"] == "no_match"
        assert result["matched"] == []
