"""GPU 真实占用查询 — 现在究竟是哪个容器在用 GPU 算力.

背景: gpu_lock.py 那把互斥锁只在 NVML 初始化窗口短暂持有(见其模块注释),不代表
"现在谁在用GPU"——ocr-vllm 启动完就 release 了锁,但推理进程仍在跑,此时锁状态是
"闲置",容易被误读成 GPU 没人用。本模块直接问驱动"现在哪些进程在算"
(nvidia-smi --query-compute-apps),把返回的 PID 解析回容器名,给出真实占用视图。

aegis-backend 自己没有 GPU 直通,借用已有 GPU 直通的 aegis-gpu-exporter 容器,经
docker exec 转发 nvidia-smi 查询;PID→容器名走宿主机 /proc/<pid>/cgroup(容器只读
挂载了 /proc:/host/proc:ro),解析 cgroup 路径里的容器长ID再核对 docker_container_list。
任何一步失败都返回空列表——这是仪表盘轮询用的只读查询,不该因为 exporter 容器重启
或某个 PID 的 cgroup 读不到就把整个请求打挂。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from obase.docker import docker_container_exec, docker_container_list
from obase.exceptions import OBaseError

log = logging.getLogger(__name__)

_EXPORTER_CONTAINER = "aegis-gpu-exporter"
_HOST_PROC = "/host/proc"
_CGROUP_DOCKER_RE = re.compile(r"docker[-/]([0-9a-f]{64})")


@dataclass
class GpuProcess:
    pid: int
    process_name: str
    memory_bytes: int
    container: str | None


def _container_for_pid(pid: int, containers: dict[str, str]) -> str | None:
    try:
        with open(f"{_HOST_PROC}/{pid}/cgroup", encoding="utf-8") as f:
            cgroup = f.read()
    except OSError:
        return None
    match = _CGROUP_DOCKER_RE.search(cgroup)
    if match is None:
        return None
    return containers.get(match.group(1))


def list_active_processes() -> list[GpuProcess]:
    """当前正在用 GPU 算力的进程,已尽量解析出容器名(解析不到则 container=None)。"""
    try:
        result = docker_container_exec(
            container_id=_EXPORTER_CONTAINER,
            command=[
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
        )
    except OBaseError:
        log.warning("failed to exec nvidia-smi in %s", _EXPORTER_CONTAINER, exc_info=True)
        return []
    if result.exit_code != 0:
        log.warning("nvidia-smi query-compute-apps failed: %s", result.stderr)
        return []

    try:
        containers = {c.container_id: c.name.lstrip("/") for c in docker_container_list(all=False)}
    except OBaseError:
        log.warning("failed to list containers for GPU pid resolution", exc_info=True)
        containers = {}

    processes: list[GpuProcess] = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            memory_bytes = int(parts[2]) * 1024 * 1024
        except ValueError:
            continue
        processes.append(
            GpuProcess(
                pid=pid,
                process_name=parts[1],
                memory_bytes=memory_bytes,
                container=_container_for_pid(pid, containers),
            )
        )
    return processes
