"""GPU 互斥闸门 API — 不经 Ollama 网关、自己直连 GPU 的外部消费方在敏感操作前拿锁.

DESIGN §5.2 单卡多项目共享(2026-07-05 事故复盘,详见 services/gpu_lock.py 与
services/ollama_gateway.py 模块注释): ocr-vllm 容器自己 docker run/start,不经过 aegis 的
Ollama 网关,所以网关的并发闸门管不到它。本路由把同一把闸门开放成 acquire/release 两个
端点,让这类外部消费方在自己的 GPU 敏感窗口(如 vLLM 引擎的 NVML 初始化)前后显式排队/
放行,与经网关的 Ollama 调用互斥。acquire/release 鉴权复用 Ollama 网关同一套共享密钥
(机器对机器)。/status 是给 aegis-console 仪表盘看的只读端点,走正常的用户登录态
(任何已登录用户可读,同 metrics.py 里host级基础设施指标的口径——不分org)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers.ollama_gateway import _verify_gateway_token
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.runtime.config import AegisSettings, get_settings
from aegis.server.services import gpu_lock as gl
from aegis.server.services import gpu_processes

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/gpu/lock", tags=["gpu-lock"])


async def _record_lock_event(conn: asyncpg.Connection, *, outcome: str, owner: str) -> None:
    """acquired/acquire_busy 计数,按 owner 分 tags——ollama 只走网关自己那两个指标
    (ollama_gateway_requests_busy/error),这里是给不经网关、直接摸这把锁的消费方
    (ocr-vllm 等)补上同等的可观测性。Best-effort:一次埋点写失败不该打断真实的锁请求。
    """
    try:
        tags = json.dumps({"owner": owner})
        await conn.execute(
            "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags) VALUES ($1, $2, $3, $4, $5::jsonb)",
            "gpu-lock",
            f"gpu_lock_{outcome}",
            1.0,
            "",
            tags,
        )
    except Exception:  # noqa: BLE001
        log.warning("failed to record gpu_lock metrics", exc_info=True)


class AcquireRequest(BaseModel):
    owner: str
    lease_sec: float = 120.0
    queue_timeout_sec: float = 60.0


class AcquireResponse(BaseModel):
    token: str
    lease_sec: float


class ReleaseRequest(BaseModel):
    token: str


@router.post("/acquire", response_model=AcquireResponse)
async def acquire(
    body: AcquireRequest,
    authorization: str | None = Header(default=None),
    cfg: AegisSettings = Depends(get_settings),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> AcquireResponse:
    """排队拿 GPU 闸门。成功后必须在 lease_sec 内调 /release,否则到期自动强制释放。"""
    _verify_gateway_token(cfg, authorization)
    try:
        lease = await gl.acquire(
            max_concurrency=cfg.ollama_gateway_max_concurrency,
            queue_timeout_sec=body.queue_timeout_sec,
            lease_sec=body.lease_sec,
            owner=body.owner,
        )
    except gl.GpuBusyError as exc:
        await _record_lock_event(conn, outcome="acquire_busy", owner=body.owner)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await _record_lock_event(conn, outcome="acquired", owner=body.owner)
    return AcquireResponse(token=lease.token, lease_sec=lease.lease_sec)


@router.post("/release")
async def release(
    body: ReleaseRequest,
    authorization: str | None = Header(default=None),
    cfg: AegisSettings = Depends(get_settings),
) -> dict[str, bool]:
    """释放一个租约。token 已过期/不存在 → released=false,调用方无需重试。"""
    _verify_gateway_token(cfg, authorization)
    return {"released": gl.release(body.token)}


@router.get("/status")
async def read_status(
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """当前谁在占用GPU —— 控制台仪表盘轮询这个。

    holders 是 NVML 初始化互斥锁的持有方,只在启动窗口短暂非空,不代表 GPU 是否
    在算——真正回答"现在谁在用GPU"的是 active_processes(nvidia-smi 实际算力占用者)。
    """
    status_data = gl.status()
    processes = await asyncio.to_thread(gpu_processes.list_active_processes)
    status_data["active_processes"] = [asdict(p) for p in processes]
    return status_data
