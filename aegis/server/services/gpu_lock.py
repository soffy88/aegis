"""GPU 互斥闸门 — 单张共享 GPU 的进程级信号量,供 Ollama 网关与外部'占用登记' API 共用.

背景(2026-07-05 事故,详见 ollama_gateway.py 模块注释): host 上 systemd Ollama 与独立的
ocr-vllm 容器各自直连同一张 GPU,ocr-vllm 启动时的 NVML 初始化窗口与 Ollama 正在进行的
推理并发撞上,触发 NVML "Unknown Error" 直接崩溃。Ollama 网关的闸门只 serialize 经网关
转发的 Ollama 调用;这道闸门本身管不到 ocr-vllm 这类完全不经网关、自己 docker run/start
的外部消费方。

本模块把同一把闸门(同一个 asyncio.Semaphore 实例)抽出来,让 ollama_gateway.py 和一个新的
HTTP 端点(api/routers/gpu_lock.py)共享 —— 外部消费方在自己的 GPU 敏感操作(如 docker run/
start 期间的 NVML 初始化)前调 acquire 排队拿锁,操作完成后 release,从而与 Ollama 的调用
互斥,而不是各自在毫无协调的情况下摸同一张卡。ollama_gateway.py 也经同一套 acquire/release
拿锁(owner=ollama:{model}),而不是直接摸信号量,这样 status() 才能看到全部持有方。

租约(lease)机制: acquire 成功后必须在 lease_sec 内 release,否则视为调用方异常退出(如
进程被杀 —— math_ocr_convert.py 里"进程被杀也要释放容器"踩过的同一类坑),到期自动释放
闸门,不让一次崩溃永久卡死全平台 GPU 访问。

status() 供控制台仪表盘展示"当前是谁在用GPU"——holders 为空即闲置。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# 进程级并发闸门,与 ollama_gateway.py 共用同一个信号量实例 —— aegis-backend 单 worker
# 运行,故这就是全平台唯一的一把闸门。用 BoundedSemaphore 而非 Semaphore: 若闸门在某租约
# 仍未释放时因 max_concurrency 变化被重建,旧租约的 release() 打到新闸门上会是一次多余的
# release —— BoundedSemaphore 会在此处抛 ValueError 让问题显形,而不是让新闸门的许可数被
# 悄悄撑大、限流形同虚设。
_gate: asyncio.BoundedSemaphore | None = None
_gate_size: int | None = None
_holders: dict[str, "Lease"] = {}
_expiry_tasks: dict[str, asyncio.Task[None]] = {}


def get_gate(max_concurrency: int) -> asyncio.BoundedSemaphore:
    global _gate, _gate_size
    if _gate is None or _gate_size != max_concurrency:
        _gate = asyncio.BoundedSemaphore(max_concurrency)
        _gate_size = max_concurrency
    return _gate


class GpuBusyError(Exception):
    """排队超过 queue_timeout_sec 仍未拿到 GPU 闸门 —— 调用方应退避重试。"""


@dataclass
class Lease:
    token: str
    owner: str
    lease_sec: float
    acquired_at: float  # time.time() epoch seconds — 供 status() 算已持有多久


async def acquire(
    *, max_concurrency: int, queue_timeout_sec: float, lease_sec: float, owner: str
) -> Lease:
    """排队拿 GPU 闸门,成功后返回租约。到期(lease_sec)未 release 则自动强制释放。"""
    gate = get_gate(max_concurrency)
    try:
        await asyncio.wait_for(gate.acquire(), timeout=queue_timeout_sec)
    except TimeoutError as exc:
        raise GpuBusyError(
            f"GPU busy: queued > {queue_timeout_sec}s waiting for the shared GPU gate"
        ) from exc

    token = secrets.token_urlsafe(16)
    lease = Lease(token=token, owner=owner, lease_sec=lease_sec, acquired_at=time.time())

    async def _auto_release() -> None:
        await asyncio.sleep(lease_sec)
        if _holders.pop(token, None) is not None:
            _expiry_tasks.pop(token, None)
            log.warning(
                "gpu lease %s (owner=%s) expired after %.0fs without release — force-releasing gate",
                token,
                owner,
                lease_sec,
            )
            try:
                gate.release()
            except ValueError:
                log.error(
                    "gpu gate over-release on expiry for lease %s (owner=%s) — gate was"
                    " resized while this lease was outstanding; permit count unaffected",
                    token,
                    owner,
                )

    _holders[token] = lease
    _expiry_tasks[token] = asyncio.ensure_future(_auto_release())
    return lease


def release(token: str) -> bool:
    """释放一个租约。token 不存在(已释放或已过期)→ 返回 False,调用方无需重试。"""
    lease = _holders.pop(token, None)
    if lease is None:
        return False
    task = _expiry_tasks.pop(token, None)
    if task is not None:
        task.cancel()
    gate = get_gate(_gate_size or 1)
    try:
        gate.release()
    except ValueError:
        log.error(
            "gpu gate over-release for lease %s (owner=%s) — gate was resized while this"
            " lease was outstanding; permit count unaffected",
            token,
            lease.owner,
        )
    return True


def status() -> dict[str, Any]:
    """当前持有方快照 —— 控制台仪表盘轮询这个,而不是直接读信号量内部状态。"""
    now = time.time()
    holders = [
        {
            "owner": lease.owner,
            "acquired_at": lease.acquired_at,
            "held_sec": round(now - lease.acquired_at, 1),
            "lease_sec": lease.lease_sec,
        }
        for lease in _holders.values()
    ]
    return {"gate_size": _gate_size or 0, "holders": holders}
