"""§9 / C-9a 演练 harness 服务端围栏.

harness 是全系统唯一被授权定期搞破坏的组件(杀容器/压测/重建演练),它自己出 bug 即事故
制造机,故 MUST 满足三条自我约束(见 DESIGN §9):
- 标签围栏:harness 只能对带 `aegis-canary` 标签的资源执行动作,且在 **API 层强制拒绝**
  越界(服务端拒绝,非 harness 自律)——本模块即该服务端断言。
- 演练窗口:可配,且 MUST 尊重变更冻结窗口(§3.2)。
- 一键中止:与 §5.3 全局急停协同。

为避免测试/生产共用实 Docker 调用,本模块把"读容器 labels"抽象为可注入的
`inspect_container_labels` 函数:默认走 oprim.docker_container_inspect,测试注入 fake。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

# 演练唯一合法标签。资源必须带它,否则服务端拒绝 harness 动作。
CANARY_LABEL = "aegis-canary"

# 可被测试注入的 inspect 函数;生产默认走 oprim。
_inspect_fn: Callable[[str], Awaitable[dict[str, str]]] | None = None


def _default_inspect(container_id: str) -> Awaitable[dict[str, str]]:
    """生产默认:调用 oprim 读取容器 labels。延迟 import 以避免无 Docker 环境崩溃。"""

    async def _run() -> dict[str, str]:
        from obase.docker import docker_container_inspect  # noqa: PLC0415

        info = await docker_container_inspect(container_id=container_id)
        # obase ContainerInfo.labels 是 dict[str, str]
        return dict(getattr(info, "labels", {}) or {})

    return _run()


async def inspect_container_labels(container_id: str) -> dict[str, str]:
    """读取目标容器的 labels。可被测试通过 `set_inspect_fn` 替换(注入 fake)。"""
    if _inspect_fn is not None:
        return await _inspect_fn(container_id)
    return await _default_inspect(container_id)


def set_inspect_fn(fn: Callable[[str], Awaitable[dict[str, str]]] | None) -> None:
    """测试注入点:传入 fake inspect 实现,或 None 恢复生产默认。"""
    global _inspect_fn  # noqa: PLW0603
    _inspect_fn = fn


def assert_canary_label(labels: dict[str, str]) -> None:
    """服务端围栏断言:资源 MUST 带 `aegis-canary` 标签,否则拒绝。

    Raises PermissionError —— 由调用方映射为 HTTP 400(fence_violation)。
    """
    if CANARY_LABEL not in (labels or {}):
        raise PermissionError(
            f"资源缺 '{CANARY_LABEL}' 标签,harness 拒绝动作 "
            f"(DESIGN §9/C-9a: 演练只能触碰带 aegis-canary 标签的资源)"
        )
