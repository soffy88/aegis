"""Tests for the Ollama gateway service (§5.2 single-GPU multi-project sharing)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aegis.server.services import gpu_lock
from aegis.server.services import ollama_gateway as gw


@pytest.fixture(autouse=True)
def _reset_gate():
    gpu_lock._gate = None
    gpu_lock._gate_size = None
    gpu_lock._holders.clear()
    gpu_lock._expiry_tasks.clear()
    yield
    gpu_lock._gate = None
    gpu_lock._gate_size = None
    gpu_lock._holders.clear()
    gpu_lock._expiry_tasks.clear()


def _mock_client(response: MagicMock | None = None, raise_exc: Exception | None = None):
    client = AsyncMock()
    if raise_exc is not None:
        client.post = AsyncMock(side_effect=raise_exc)
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        client.post = AsyncMock(return_value=response)
        client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


def _ok_response(payload: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


@pytest.mark.asyncio
async def test_list_models_success():
    ctx, _ = _mock_client(_ok_response({"models": ["a"]}))
    with patch("httpx.AsyncClient", return_value=ctx):
        result = await gw.list_models(base_url="http://ollama:11434")
    assert result == {"models": ["a"]}


@pytest.mark.asyncio
async def test_list_models_unreachable_raises_upstream_error():
    ctx, _ = _mock_client(raise_exc=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", return_value=ctx):
        with pytest.raises(gw.GatewayUpstreamError):
            await gw.list_models(base_url="http://ollama:11434")


@pytest.mark.asyncio
async def test_generate_forces_stream_false():
    captured = {}

    async def fake_post(url, json):
        captured["url"] = url
        captured["json"] = json
        return _ok_response({"response": "hi"})

    ctx, client = _mock_client(_ok_response({"response": "hi"}))
    client.post = AsyncMock(side_effect=fake_post)
    with patch("httpx.AsyncClient", return_value=ctx):
        result = await gw.generate(
            base_url="http://ollama:11434",
            payload={"model": "qwen2.5:7b", "prompt": "hi", "stream": True},
            max_concurrency=1,
            queue_timeout_sec=5,
        )
    assert result == {"response": "hi"}
    assert captured["json"]["stream"] is False  # 强制非流式
    assert captured["url"] == "http://ollama:11434/api/generate"


@pytest.mark.asyncio
async def test_generate_upstream_http_error():
    resp = MagicMock()
    resp.status_code = 500
    err = httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
    ctx, client = _mock_client()
    client.post = AsyncMock(side_effect=err)
    with patch("httpx.AsyncClient", return_value=ctx):
        with pytest.raises(gw.GatewayUpstreamError) as exc_info:
            await gw.generate(
                base_url="http://ollama:11434",
                payload={"model": "m", "prompt": "p"},
                max_concurrency=1,
                queue_timeout_sec=5,
            )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_concurrency_gate_serializes_requests():
    """第二个请求必须等第一个释放闸门才能拿到锁(单卡=串行,不能同时穿透到 GPU)。"""
    order: list[str] = []
    release_first = asyncio.Event()

    async def slow_post(url, json):
        order.append("start-" + json["marker"])
        if json["marker"] == "first":
            await release_first.wait()
        order.append("end-" + json["marker"])
        return _ok_response({"ok": True})

    ctx, client = _mock_client()
    client.post = AsyncMock(side_effect=slow_post)

    with patch("httpx.AsyncClient", return_value=ctx):
        first = asyncio.create_task(
            gw.generate(
                base_url="http://ollama:11434",
                payload={"model": "m", "prompt": "p", "marker": "first"},
                max_concurrency=1,
                queue_timeout_sec=5,
            )
        )
        await asyncio.sleep(0.05)  # 确保 first 已经拿到闸门、正在"跑"
        holders = gpu_lock.status()["holders"]
        assert len(holders) == 1 and holders[0]["owner"] == "ollama:m"  # 持有方对gpu_lock可见
        second = asyncio.create_task(
            gw.generate(
                base_url="http://ollama:11434",
                payload={"model": "m", "prompt": "p", "marker": "second"},
                max_concurrency=1,
                queue_timeout_sec=5,
            )
        )
        await asyncio.sleep(0.05)
        assert order == ["start-first"]  # second 还卡在闸门外,没能 start
        release_first.set()
        await first
        await second

    assert order == ["start-first", "end-first", "start-second", "end-second"]


@pytest.mark.asyncio
async def test_gate_timeout_raises_busy_error():
    async def hang_post(url, json):
        await asyncio.sleep(10)
        return _ok_response({})

    ctx, client = _mock_client()
    client.post = AsyncMock(side_effect=hang_post)

    with patch("httpx.AsyncClient", return_value=ctx):
        holder = asyncio.create_task(
            gw.generate(
                base_url="http://ollama:11434",
                payload={"model": "m", "prompt": "p"},
                max_concurrency=1,
                queue_timeout_sec=5,
            )
        )
        await asyncio.sleep(0.05)
        with pytest.raises(gw.GatewayBusyError):
            await gw.generate(
                base_url="http://ollama:11434",
                payload={"model": "m", "prompt": "p2"},
                max_concurrency=1,
                queue_timeout_sec=0.05,
            )
        holder.cancel()
