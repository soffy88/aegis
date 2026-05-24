"""服务层 LLM 调用薄包装 (走 obase.ProviderRegistry).

硬约束 (Step 15 §4.2):
- ✅ 服务层用此包装做轻量 LLM 调用 (e.g. router 内一次性翻译 / 摘要)
- ❌ 不传给 omodul (omodul 自己通过 ProviderRegistry 取 LLMCaller)
- ❌ 不实现 retry / cost tracking 自己 (obase 已有)
"""

from __future__ import annotations

import logging
from typing import Any

from obase import ProviderRegistry

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)


def _settings() -> AegisSettings:
    return AegisSettings()


async def call_llm(
    *,
    messages: list[dict[str, Any]],
    model: str | None = None,
    provider: str | None = None,
    max_tokens: int = 2048,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """轻量 LLM 调用 (服务层用, 不给 omodul).

    Returns:
        dict 含 content / stop_reason / usage (provider 标准格式)

    Raises:
        obase.ProviderNotFoundError: provider 未注册
    """
    settings = _settings()
    provider = provider or settings.llm_provider
    model = model or settings.llm_model_default
    caller = ProviderRegistry.get_caller(provider, model)
    return caller(messages=messages, tools=tools, max_tokens=max_tokens)


async def call_anthropic(
    messages: list[dict[str, Any]],
    model: str = "claude-sonnet-4-6",
    **kwargs: Any,
) -> dict[str, Any]:
    """[Deprecated] 用 call_llm 代替."""
    log.warning("call_anthropic is deprecated, use call_llm instead")
    return await call_llm(messages=messages, model=model, provider="anthropic", **kwargs)
