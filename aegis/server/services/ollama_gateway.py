"""Ollama 网关 — aegis 接管为单张共享 GPU 的唯一入口 (DESIGN §5.2 单卡多项目共享).

背景(2026-07-05 事故): 本机只有一块物理 GPU,host 上 systemd 管理的 Ollama 长期占用它
服务真实流量。另一独立项目(ocr-vllm)启动时并发查询/占用同一张卡,触发 NVML
"Unknown Error" 直接崩溃,且让 nvidia-smi 对其它进程也报同样错误——这是多个独立进程
各自直连 GPU 的必然后果(docker 本身不对 GPU 设备做互斥)。

修法: 其它项目不再各自直连 Ollama/GPU,一律经此网关转发。网关内部用一个进程级并发闸门
serialize 对底层 Ollama 的实际调用(默认 1 = 同一时刻全平台只有一个请求真正触达 GPU),
排队超时则拒绝(503)而不是让多个请求一起砸向驱动。aegis-backend 以单 worker 运行
(见 Dockerfile.prod 注释),故一个进程内的 asyncio.Semaphore 就是全局的、正确的。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# 进程级并发闸门。在模块加载时按当前配置的上限创建一次；aegis-backend 单 worker 运行,
# 故这就是全平台唯一的一把闸门,不存在多进程各自持有互不可见的信号量的问题。
_gate: asyncio.Semaphore | None = None
_gate_size: int | None = None


def _get_gate(max_concurrency: int) -> asyncio.Semaphore:
    global _gate, _gate_size
    if _gate is None or _gate_size != max_concurrency:
        _gate = asyncio.Semaphore(max_concurrency)
        _gate_size = max_concurrency
    return _gate


class GatewayBusyError(Exception):
    """排队超过 queue_timeout_sec 仍未拿到闸门 —— GPU 繁忙,调用方应退避重试。"""


class GatewayUpstreamError(Exception):
    """Ollama 本身返回错误或不可达(含 NVML/驱动故障)。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def list_models(*, base_url: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    """GET /api/tags 透传 —— 轻量元数据查询,不占并发闸门(不触达 GPU)。"""
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except httpx.HTTPStatusError as exc:
        raise GatewayUpstreamError(str(exc), status_code=exc.response.status_code) from exc
    except httpx.HTTPError as exc:
        raise GatewayUpstreamError(f"ollama unreachable: {exc}") from exc


async def _proxy_gated(
    *,
    base_url: str,
    path: str,
    payload: dict[str, Any],
    max_concurrency: int,
    queue_timeout_sec: float,
    request_timeout_sec: float,
) -> dict[str, Any]:
    """经并发闸门转发一次真实触达 GPU 的调用(generate/chat/embed)。

    闸门持有时长覆盖整次生成(强制 stream=false),这正是我们要的语义——闸门必须罩住
    GPU 实际忙碌的整个区间,而不是提前释放。排队超时 → GatewayBusyError(503);
    Ollama 返回非 2xx 或不可达 → GatewayUpstreamError。
    """
    gate = _get_gate(max_concurrency)
    try:
        await asyncio.wait_for(gate.acquire(), timeout=queue_timeout_sec)
    except TimeoutError as exc:
        raise GatewayBusyError(
            f"GPU busy: queued > {queue_timeout_sec}s waiting for the shared Ollama gate"
        ) from exc

    try:
        body = {**payload, "stream": False}
        async with httpx.AsyncClient(timeout=request_timeout_sec) as client:
            resp = await client.post(f"{base_url}{path}", json=body)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except httpx.HTTPStatusError as exc:
        raise GatewayUpstreamError(str(exc), status_code=exc.response.status_code) from exc
    except httpx.HTTPError as exc:
        raise GatewayUpstreamError(f"ollama unreachable: {exc}") from exc
    finally:
        gate.release()


async def generate(
    *,
    base_url: str,
    payload: dict[str, Any],
    max_concurrency: int,
    queue_timeout_sec: float,
    request_timeout_sec: float = 300.0,
) -> dict[str, Any]:
    """POST /api/generate 经并发闸门转发。"""
    return await _proxy_gated(
        base_url=base_url,
        path="/api/generate",
        payload=payload,
        max_concurrency=max_concurrency,
        queue_timeout_sec=queue_timeout_sec,
        request_timeout_sec=request_timeout_sec,
    )


async def chat(
    *,
    base_url: str,
    payload: dict[str, Any],
    max_concurrency: int,
    queue_timeout_sec: float,
    request_timeout_sec: float = 300.0,
) -> dict[str, Any]:
    """POST /api/chat 经并发闸门转发。"""
    return await _proxy_gated(
        base_url=base_url,
        path="/api/chat",
        payload=payload,
        max_concurrency=max_concurrency,
        queue_timeout_sec=queue_timeout_sec,
        request_timeout_sec=request_timeout_sec,
    )
