"""Runbook service — YAML-defined operational runbooks with dry-run + approval.

匹配逻辑走 oskill.runbook_match (主库), 执行逻辑保留服务层.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from oskill import RunbookMatchResult, runbook_match
from pydantic import BaseModel, Field

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)


class StepType(StrEnum):
    shell = "shell"
    docker = "docker"
    http = "http"


class RunbookStep(BaseModel):
    name: str
    type: StepType
    command: str
    timeout: int = 60


class Runbook(BaseModel):
    name: str
    description: str = ""
    trigger: str = "manual"
    steps: list[RunbookStep]
    requires_approval: bool = True


class ExecutionStatus(StrEnum):
    pending = "pending"
    dry_run = "dry_run"
    awaiting_approval = "awaiting_approval"
    running = "running"
    completed = "completed"
    failed = "failed"


class StepResult(BaseModel):
    step_name: str
    status: str = "pending"
    output: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunbookExecution(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    runbook_name: str
    status: ExecutionStatus = ExecutionStatus.pending
    dry_run: bool = True
    steps: list[StepResult] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    approved_at: datetime | None = None
    completed_at: datetime | None = None


# In-memory caches. _runbooks is a YAML-derived catalog (rebuilt at startup, fine to
# keep in memory). _executions is a write-through cache over durable JSON files so
# execution history survives restarts (see _persist / _load_execution).
_runbooks: dict[str, Runbook] = {}
_executions: dict[str, RunbookExecution] = {}


def _exec_dir() -> Path:
    d = Path(AegisSettings().data_dir) / "runbook_executions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _persist(execution: RunbookExecution) -> None:
    """Write-through a full execution snapshot to disk. Best-effort (logs on failure)."""
    try:
        path = _exec_dir() / f"{execution.id}.json"
        path.write_text(execution.model_dump_json())
    except Exception:
        log.warning("Failed to persist runbook execution %s", execution.id)


def _load_execution(exec_id: str) -> RunbookExecution | None:
    path = _exec_dir() / f"{exec_id}.json"
    if not path.exists():
        return None
    try:
        execution = RunbookExecution.model_validate_json(path.read_text())
        _executions[exec_id] = execution  # warm the cache
        return execution
    except Exception:
        log.warning("Failed to load persisted runbook execution %s", exec_id)
        return None


def load_runbooks() -> None:
    """Load runbooks from YAML files in runbooks directory."""
    settings = AegisSettings()
    runbooks_dir = Path(settings.data_dir) / "runbooks"
    runbooks_dir.mkdir(parents=True, exist_ok=True)

    for pattern in ("*.yml", "*.yaml"):
        for f in runbooks_dir.glob(pattern):
            try:
                data = yaml.safe_load(f.read_text())
                rb = Runbook(**data)
                _runbooks[rb.name] = rb
            except Exception:
                log.warning("Failed to parse runbook: %s", f)


def list_runbooks() -> list[Runbook]:
    return list(_runbooks.values())


def get_runbook(name: str) -> Runbook | None:
    return _runbooks.get(name)


def get_execution(exec_id: str) -> RunbookExecution | None:
    return _executions.get(exec_id) or _load_execution(exec_id)


def match_runbook(root_cause: dict, min_score: float = 0.7) -> RunbookMatchResult:
    """匹配 root_cause 到 runbook (走主库 oskill.runbook_match)."""
    available = [rb.model_dump() for rb in _runbooks.values()]
    return runbook_match(
        root_cause=root_cause,
        available_plugins=available,
        min_match_score=min_score,
    )


async def execute_runbook(name: str, dry_run: bool = True) -> RunbookExecution:
    """Execute a runbook (dry_run or live). 执行逻辑保留服务层."""
    rb = _runbooks.get(name)
    if not rb:
        raise ValueError(f"Runbook '{name}' not found")

    execution = RunbookExecution(
        runbook_name=name,
        dry_run=dry_run,
        status=ExecutionStatus.dry_run if dry_run else ExecutionStatus.running,
        steps=[StepResult(step_name=s.name) for s in rb.steps],
    )
    _executions[execution.id] = execution
    _persist(execution)

    if dry_run:
        for i, step in enumerate(rb.steps):
            execution.steps[i].status = "would_execute"
            execution.steps[i].output = f"[DRY RUN] Would run: {step.command}"
            execution.steps[i].started_at = datetime.now(tz=UTC)
            execution.steps[i].finished_at = datetime.now(tz=UTC)
        if rb.requires_approval:
            execution.status = ExecutionStatus.awaiting_approval
        else:
            execution.status = ExecutionStatus.completed
            execution.completed_at = datetime.now(tz=UTC)
    else:
        for i, step in enumerate(rb.steps):
            execution.steps[i].started_at = datetime.now(tz=UTC)
            execution.steps[i].status = "running"
            try:
                if step.type == StepType.docker:
                    from obase.docker import docker_container_restart  # noqa: PLC0415

                    parts = step.command.split()
                    if parts[0] == "restart" and len(parts) > 1:
                        docker_container_restart(container_id=parts[1])
                        execution.steps[i].output = f"Restarted {parts[1]}"
                    else:
                        execution.steps[i].output = f"Unknown docker command: {step.command}"
                elif step.type == StepType.shell:
                    proc = await asyncio.create_subprocess_shell(
                        step.command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=step.timeout
                    )
                    execution.steps[i].output = (stdout or b"").decode()[:1000]
                else:
                    execution.steps[i].output = f"Step type '{step.type}' not implemented"
                execution.steps[i].status = "completed"
            except Exception as exc:
                execution.steps[i].status = "failed"
                execution.steps[i].output = str(exc)
            execution.steps[i].finished_at = datetime.now(tz=UTC)

        all_ok = all(s.status == "completed" for s in execution.steps)
        execution.status = ExecutionStatus.completed if all_ok else ExecutionStatus.failed
        execution.completed_at = datetime.now(tz=UTC)

    _persist(execution)
    return execution


def approve_execution(exec_id: str) -> RunbookExecution | None:
    """Approve a pending execution, triggering live run."""
    execution = _executions.get(exec_id) or _load_execution(exec_id)
    if not execution or execution.status != ExecutionStatus.awaiting_approval:
        return None
    execution.approved_at = datetime.now(tz=UTC)
    execution.status = ExecutionStatus.running
    _persist(execution)
    return execution
