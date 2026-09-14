"""§9 / C-9a: S1 演练 harness 服务端标签围栏测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aegis.server.services.drill import (
    CANARY_LABEL,
    assert_canary_label,
    inspect_container_labels,
    set_inspect_fn,
)

_ORG = uuid4()


@pytest.fixture(autouse=True)
def _reset_inspect():
    set_inspect_fn(None)
    yield
    set_inspect_fn(None)


def test_assert_canary_label_passes_with_label():
    """目标带 aegis-canary 标签 → 围栏放行。"""
    assert_canary_label({CANARY_LABEL: "true", "name": "svc"})  # 不抛


def test_assert_canary_label_rejects_without_label():
    """目标缺 aegis-canary 标签 → 服务端拒绝 (C-9a)。"""
    with pytest.raises(PermissionError) as exc:
        assert_canary_label({"name": "svc"})
    assert "aegis-canary" in str(exc.value)
    assert "拒绝" in str(exc.value)


def test_assert_canary_label_rejects_empty():
    """无标签 → 拒绝。"""
    with pytest.raises(PermissionError):
        assert_canary_label({})


@pytest.mark.asyncio
async def test_inspect_uses_injected_fn():
    """测试可注入 fake inspect,避免依赖真 Docker。"""
    async def _fake(container_id):
        return {CANARY_LABEL: "true"}

    set_inspect_fn(_fake)
    labels = await inspect_container_labels("canary-1")
    assert labels == {CANARY_LABEL: "true"}


@pytest.mark.asyncio
async def test_inspect_injectable_for_router():
    """API 层:非 canary 标签容器被 400 拒绝(端到端)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.server.api.deps import get_db_conn
    from aegis.server.api.routers import autoheal as autoheal_router
    from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

    async def _no_label(container_id):
        return {"name": "prod-svc"}  # 缺 aegis-canary

    set_inspect_fn(_no_label)

    app = FastAPI()
    app.include_router(autoheal_router.router)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    async def _db():
        yield conn

    async def _user():
        return UserContext(
            user_id=uuid4(),
            email="admin@x.com",
            orgs=[OrgInToken(org_id=_ORG, slug="t", role="admin")],
        )

    app.dependency_overrides[get_db_conn] = _db
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app, raise_server_exceptions=False)

    # 演练对象非 canary → 400 fence_violation
    r = client.post(
        f"/api/v1/orgs/{_ORG}/autoheal/drill",
        json={"container_id": "prod-svc", "action": "kill", "reason": "test"},
    )
    assert r.status_code == 400
    assert "drill_fence_violation" in r.json()["detail"]
