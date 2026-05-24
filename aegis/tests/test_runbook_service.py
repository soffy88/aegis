"""Tests for C0c-3: runbook → oskill.runbook_match integration."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest

from aegis.server.services.runbook import (
    _runbooks,
    execute_runbook,
    match_runbook,
)


@pytest.fixture(autouse=True)
def _reset_runbooks() -> None:
    """Clear runbook state between tests."""
    _runbooks.clear()


@pytest.fixture
def _loaded_runbook() -> None:
    """Load a sample runbook into the registry."""
    from aegis.server.services.runbook import Runbook, RunbookStep, StepType

    _runbooks["restart-nginx"] = Runbook(
        name="restart-nginx",
        description="Restart nginx container",
        trigger="alert:nginx_unhealthy",
        steps=[RunbookStep(name="restart", type=StepType.docker, command="restart nginx")],
    )


@pytest.mark.usefixtures("_loaded_runbook")
def test_match_uses_oskill_runbook_match() -> None:
    """match_runbook delegates to oskill.runbook_match."""
    fake_result = MagicMock()
    fake_result.matched_plugin = {"name": "restart-nginx"}
    fake_result.match_score = 0.9
    fake_result.alternative_plugins = []

    with mock.patch(
        "aegis.server.services.runbook.runbook_match",
        return_value=fake_result,
    ) as m:
        result = match_runbook(root_cause={"type": "container_unhealthy", "container": "nginx"})

    m.assert_called_once()
    call_kwargs = m.call_args.kwargs
    assert call_kwargs["root_cause"] == {"type": "container_unhealthy", "container": "nginx"}
    assert call_kwargs["min_match_score"] == 0.7
    assert len(call_kwargs["available_plugins"]) == 1
    assert result.matched_plugin == {"name": "restart-nginx"}


def test_match_no_runbooks_returns_none() -> None:
    """No runbooks loaded → empty available_plugins list."""
    fake_result = MagicMock()
    fake_result.matched_plugin = None
    fake_result.match_score = 0.0

    with mock.patch(
        "aegis.server.services.runbook.runbook_match",
        return_value=fake_result,
    ):
        result = match_runbook(root_cause={"type": "unknown"})

    assert result.matched_plugin is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_loaded_runbook")
async def test_execute_dry_run() -> None:
    """Dry-run execution does not call oprim."""
    execution = await execute_runbook("restart-nginx", dry_run=True)
    assert execution.status == "awaiting_approval"
    assert execution.steps[0].status == "would_execute"
