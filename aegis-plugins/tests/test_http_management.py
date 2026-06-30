"""Tests for the HTTP-management remediation plugins."""

from __future__ import annotations

import pytest
from aegis_autoheal_sdk import ActionResultStatus, AutoHealContext, ServiceInfo, Severity

from aegis_plugins.plugins.http_management import (
    HealthcheckExternalPlugin,
    ReloadConfigPlugin,
    ResetCircuitBreakerPlugin,
)


class _Svc(ServiceInfo):
    @property
    def name(self) -> str:
        return "svc"

    @property
    def health(self) -> str:
        return "down"

    @property
    def version(self) -> str | None:
        return None


class _Ctx(AutoHealContext):
    def __init__(self, payload: dict, *, http_codes: list[int]) -> None:
        self._payload = payload
        self._codes = list(http_codes)
        self.calls: list[str] = []

    @property
    def service(self) -> ServiceInfo:
        return _Svc()

    @property
    def alert_payload(self) -> dict:
        return self._payload

    @property
    def org_environment(self) -> Severity:
        return Severity.PRODUCTION

    @property
    def trace_id(self) -> str:
        return "t"

    async def http_get(self, url: str, **kwargs: object) -> dict:
        self.calls.append(url)
        code = self._codes.pop(0) if self._codes else 200
        return {"status_code": code}


@pytest.mark.asyncio
async def test_reload_config_hits_reload_path_and_succeeds() -> None:
    ctx = _Ctx({"reload_url": "http://svc:8080"}, http_codes=[200])
    p = ReloadConfigPlugin()
    assert await p.pre_check(ctx) is True
    result = await p.execute(ctx)
    assert result.status is ActionResultStatus.OK
    assert ctx.calls == ["http://svc:8080/reload"]


@pytest.mark.asyncio
async def test_execute_fails_on_4xx() -> None:
    ctx = _Ctx({"breaker_url": "http://svc"}, http_codes=[503])
    result = await ResetCircuitBreakerPlugin().execute(ctx)
    assert result.status is ActionResultStatus.FAILED


@pytest.mark.asyncio
async def test_pre_check_false_without_url() -> None:
    ctx = _Ctx({}, http_codes=[])
    assert await ReloadConfigPlugin().pre_check(ctx) is False


@pytest.mark.asyncio
async def test_execute_without_url_fails() -> None:
    ctx = _Ctx({}, http_codes=[])
    result = await ReloadConfigPlugin().execute(ctx)
    assert result.status is ActionResultStatus.FAILED
    assert "reload_url" in result.detail


@pytest.mark.asyncio
async def test_healthcheck_probes_base_url_directly() -> None:
    ctx = _Ctx({"probe_url": "http://dep/health"}, http_codes=[200])
    result = await HealthcheckExternalPlugin().execute(ctx)
    assert result.status is ActionResultStatus.OK
    assert ctx.calls == ["http://dep/health"]  # action_path is empty


@pytest.mark.asyncio
async def test_post_verify_checks_health() -> None:
    ctx = _Ctx({"reload_url": "http://svc"}, http_codes=[200])
    assert await ReloadConfigPlugin().post_verify(ctx) is True
    assert ctx.calls[-1] == "http://svc/health"


@pytest.mark.asyncio
async def test_rollback_is_skip() -> None:
    ctx = _Ctx({"reload_url": "http://svc"}, http_codes=[])
    rb = await ReloadConfigPlugin().rollback(ctx)
    assert rb.status is ActionResultStatus.SKIPPED


def test_validate_config_passes() -> None:
    for cls in (HealthcheckExternalPlugin, ReloadConfigPlugin, ResetCircuitBreakerPlugin):
        cls.validate_config()  # must not raise
