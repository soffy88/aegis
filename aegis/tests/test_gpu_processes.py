"""Unit tests for gpu_processes — resolving nvidia-smi compute-app PIDs to container names."""

from __future__ import annotations

from unittest import mock

from obase.docker.client import ContainerExecResult, ContainerInfo
from obase.exceptions import OBaseConnectionError

from aegis.server.services import gpu_processes as gp


def _exec_result(stdout: str, exit_code: int = 0) -> ContainerExecResult:
    return ContainerExecResult(
        container_id="exporter",
        command=["nvidia-smi"],
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        elapsed_ms=1,
    )


def _container(container_id: str, name: str) -> ContainerInfo:
    return ContainerInfo(
        container_id=container_id,
        name=name,
        image="img",
        state="running",
        status="Up",
        started_at=None,
        finished_at=None,
        exit_code=None,
        health=None,
        restart_count=0,
        labels={},
        ports=[],
        mounts=[],
    )


def test_resolves_pid_to_container_via_cgroup(tmp_path, monkeypatch):
    cid = "ab" * 32
    proc_dir = tmp_path / "67410"
    proc_dir.mkdir()
    (proc_dir / "cgroup").write_text(f"0::/system.slice/docker-{cid}.scope\n")
    monkeypatch.setattr(gp, "_HOST_PROC", str(tmp_path))

    with (
        mock.patch.object(
            gp,
            "docker_container_exec",
            return_value=_exec_result("67410, VLLM::EngineCore, 9074\n"),
        ),
        mock.patch.object(gp, "docker_container_list", return_value=[_container(cid, "ocr-vllm")]),
    ):
        result = gp.list_active_processes()

    assert result == [
        gp.GpuProcess(
            pid=67410,
            process_name="VLLM::EngineCore",
            memory_bytes=9074 * 1024 * 1024,
            container="ocr-vllm",
        )
    ]


def test_returns_none_container_when_cgroup_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "_HOST_PROC", str(tmp_path))  # pid dir doesn't exist

    with (
        mock.patch.object(
            gp, "docker_container_exec", return_value=_exec_result("999, some-proc, 100\n")
        ),
        mock.patch.object(gp, "docker_container_list", return_value=[]),
    ):
        result = gp.list_active_processes()

    assert result == [
        gp.GpuProcess(
            pid=999, process_name="some-proc", memory_bytes=100 * 1024 * 1024, container=None
        )
    ]


def test_empty_when_no_compute_apps():
    with (
        mock.patch.object(gp, "docker_container_exec", return_value=_exec_result("")),
        mock.patch.object(gp, "docker_container_list", return_value=[]),
    ):
        assert gp.list_active_processes() == []


def test_empty_when_nvidia_smi_fails():
    with mock.patch.object(gp, "docker_container_exec", return_value=_exec_result("", exit_code=6)):
        assert gp.list_active_processes() == []


def test_empty_when_exec_raises():
    with mock.patch.object(
        gp, "docker_container_exec", side_effect=OBaseConnectionError("no docker")
    ):
        assert gp.list_active_processes() == []
