"""Tests for the shared GPU mutex (§5.2 single-GPU multi-project sharing)."""

from __future__ import annotations

import asyncio

import pytest

from aegis.server.services import gpu_lock as gl


@pytest.fixture(autouse=True)
def _reset_gate():
    gl._gate = None
    gl._gate_size = None
    gl._holders.clear()
    gl._expiry_tasks.clear()
    yield
    gl._gate = None
    gl._gate_size = None
    gl._holders.clear()
    gl._expiry_tasks.clear()


@pytest.mark.asyncio
async def test_acquire_then_release_frees_the_gate():
    lease = await gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="test")
    st = gl.status()
    assert len(st["holders"]) == 1
    assert st["holders"][0]["owner"] == "test"

    released = gl.release(lease.token)
    assert released is True
    assert gl.status()["holders"] == []

    # gate is free again — a second acquire should not block.
    lease2 = await asyncio.wait_for(
        gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="test"),
        timeout=1,
    )
    gl.release(lease2.token)


def test_release_unknown_token_returns_false():
    assert gl.release("does-not-exist") is False


@pytest.mark.asyncio
async def test_second_acquire_blocks_until_first_released():
    lease = await gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="a")

    with pytest.raises(gl.GpuBusyError):
        await gl.acquire(max_concurrency=1, queue_timeout_sec=0.05, lease_sec=10, owner="b")

    gl.release(lease.token)
    lease2 = await asyncio.wait_for(
        gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="b"),
        timeout=1,
    )
    gl.release(lease2.token)


@pytest.mark.asyncio
async def test_expired_lease_auto_releases_gate():
    """调用方拿到锁后崩溃、从未 release —— 租约到期必须自动放行,不能永久卡死GPU。"""
    lease = await gl.acquire(
        max_concurrency=1, queue_timeout_sec=1, lease_sec=0.05, owner="crashed"
    )
    del lease  # 模拟调用方异常退出,再也不会来 release

    lease2 = await asyncio.wait_for(
        gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="next"),
        timeout=1,
    )
    gl.release(lease2.token)


@pytest.mark.asyncio
async def test_ollama_gateway_and_gpu_lock_share_the_same_gate():
    """ocr-vllm 走 gpu_lock 拿锁时, ollama_gateway 的调用必须真的被挡住 —— 这是这次改动的
    唯一目的:两条完全不同的调用路径必须互斥同一张 GPU。"""
    from aegis.server.services import ollama_gateway as ogw

    lease = await gl.acquire(max_concurrency=1, queue_timeout_sec=1, lease_sec=10, owner="ocr-vllm")

    gate = ogw.gpu_lock.get_gate(1)
    assert gate is gl.get_gate(1)
    assert gate.locked()

    gl.release(lease.token)
    assert not gate.locked()
