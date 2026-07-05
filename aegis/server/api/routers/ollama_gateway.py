"""Ollama 网关 API — 其它项目 MUST 经此调用共享 GPU 上的 Ollama,而非各自直连.

DESIGN §5.2 单卡多项目共享:docker 本身不对 GPU 设备做互斥,多个独立进程各自直连会
互相干扰甚至崩溃(2026-07-05 ocr-vllm 崩溃事故)。此路由把 aegis 变成唯一入口,内部
经并发闸门 serialize 对底层 Ollama 的真实调用。鉴权用共享密钥(机器对机器场景,不是
org JWT——调用方是同宿主机上的独立项目,没有 aegis 账号),同 metrics /ingest 的
agent_token 模式。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from aegis.server.runtime.config import AegisSettings, get_settings
from aegis.server.services import ollama_gateway as gw

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/llm/ollama", tags=["ollama-gateway"])


def _verify_gateway_token(cfg: AegisSettings, authorization: str | None) -> None:
    """校验共享密钥。未配置 ollama_gateway_token → 跳过校验(仅限内网场景)。"""
    if not cfg.ollama_gateway_token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, cfg.ollama_gateway_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid gateway token")


def _require_configured(cfg: AegisSettings) -> str:
    if not cfg.ollama_base_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Ollama gateway not configured (AEGIS_OLLAMA_BASE_URL unset)",
        )
    return cfg.ollama_base_url


class GenerateRequest(BaseModel):
    model: str
    prompt: str
    options: dict[str, Any] | None = None
    format: str | dict[str, Any] | None = None


class ChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    options: dict[str, Any] | None = None
    format: str | dict[str, Any] | None = None


@router.get("/tags")
async def list_models(
    authorization: str | None = Header(default=None),
    cfg: AegisSettings = Depends(get_settings),
) -> dict[str, Any]:
    """可用模型列表 —— 元数据查询,不占并发闸门。"""
    _verify_gateway_token(cfg, authorization)
    base_url = _require_configured(cfg)
    try:
        return await gw.list_models(base_url=base_url)
    except gw.GatewayUpstreamError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/generate")
async def generate(
    body: GenerateRequest,
    authorization: str | None = Header(default=None),
    cfg: AegisSettings = Depends(get_settings),
) -> dict[str, Any]:
    """转发 /api/generate,经并发闸门 serialize(单卡多项目共享,§5.2)。"""
    _verify_gateway_token(cfg, authorization)
    base_url = _require_configured(cfg)
    try:
        return await gw.generate(
            base_url=base_url,
            payload=body.model_dump(exclude_none=True),
            max_concurrency=cfg.ollama_gateway_max_concurrency,
            queue_timeout_sec=cfg.ollama_gateway_queue_timeout_sec,
        )
    except gw.GatewayBusyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except gw.GatewayUpstreamError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/chat")
async def chat(
    body: ChatRequest,
    authorization: str | None = Header(default=None),
    cfg: AegisSettings = Depends(get_settings),
) -> dict[str, Any]:
    """转发 /api/chat,经并发闸门 serialize(单卡多项目共享,§5.2)。"""
    _verify_gateway_token(cfg, authorization)
    base_url = _require_configured(cfg)
    try:
        return await gw.chat(
            base_url=base_url,
            payload=body.model_dump(exclude_none=True),
            max_concurrency=cfg.ollama_gateway_max_concurrency,
            queue_timeout_sec=cfg.ollama_gateway_queue_timeout_sec,
        )
    except gw.GatewayBusyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except gw.GatewayUpstreamError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
