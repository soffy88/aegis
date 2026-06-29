"""Tests for the aegis-autoheal-sdk contract."""

from __future__ import annotations

import pytest

from aegis_autoheal_sdk import (
    ActionResult,
    ActionResultStatus,
    AutoHealContext,
    AutoHealPlugin,
    ServiceInfo,
    Severity,
)


# ── ActionResult factories ──────────────────────────────────────────────────────


def test_ok_factory() -> None:
    r = ActionResult.ok("done")
    assert r.status is ActionResultStatus.OK
    assert r.detail == "done"
    assert r.is_success is True
    assert r.escalate_to is None


def test_failed_factory() -> None:
    r = ActionResult.failed("boom")
    assert r.status is ActionResultStatus.FAILED
    assert r.is_success is False


def test_escalate_factory_sets_target() -> None:
    r = ActionResult.escalate(to="human", detail="needs a person")
    assert r.status is ActionResultStatus.ESCALATE
    assert r.escalate_to == "human"


def test_skipped_factory() -> None:
    assert ActionResult.skipped("noop").status is ActionResultStatus.SKIPPED


# ── enums ───────────────────────────────────────────────────────────────────────


def test_severity_members() -> None:
    assert {s.value for s in Severity} == {"dev", "staging", "production"}


def test_status_members() -> None:
    assert {s.value for s in ActionResultStatus} == {"ok", "failed", "escalate", "skipped"}


# ── AutoHealPlugin ──────────────────────────────────────────────────────────────


class _GoodPlugin(AutoHealPlugin):
    name = "good"
    version = "1.0.0"
    matches_alert = "x"
    description = "ok"
    rate_limit = "2/5min"

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.ok("ran")


def test_validate_config_passes_for_well_formed_plugin() -> None:
    _GoodPlugin.validate_config()  # must not raise


def test_validate_config_rejects_missing_name() -> None:
    class _Bad(AutoHealPlugin):
        version = "1.0.0"
        matches_alert = "x"

    with pytest.raises(ValueError, match="name"):
        _Bad.validate_config()


def test_validate_config_rejects_bad_rate_limit() -> None:
    class _Bad(AutoHealPlugin):
        name = "b"
        version = "1.0.0"
        matches_alert = "x"
        rate_limit = "nonsense"

    with pytest.raises(ValueError, match="rate_limit"):
        _Bad.validate_config()


@pytest.mark.asyncio
async def test_default_lifecycle_methods() -> None:
    p = _GoodPlugin()
    ctx = object()  # not used by defaults
    assert await p.pre_check(ctx) is True  # type: ignore[arg-type]
    assert await p.post_verify(ctx) is True  # type: ignore[arg-type]
    rb = await p.rollback(ctx)  # type: ignore[arg-type]
    assert rb.status is ActionResultStatus.SKIPPED


@pytest.mark.asyncio
async def test_base_execute_raises_until_overridden() -> None:
    class _NoExec(AutoHealPlugin):
        name = "n"
        version = "1.0.0"
        matches_alert = "x"

    with pytest.raises(NotImplementedError):
        await _NoExec().execute(object())  # type: ignore[arg-type]


# ── abstract bases require implementation ───────────────────────────────────────


def test_serviceinfo_is_abstract() -> None:
    with pytest.raises(TypeError):
        ServiceInfo()  # type: ignore[abstract]


def test_context_is_abstract() -> None:
    with pytest.raises(TypeError):
        AutoHealContext()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_context_capability_defaults_raise() -> None:
    class _Ctx(AutoHealContext):
        @property
        def service(self) -> ServiceInfo: ...  # type: ignore[empty-body]
        @property
        def alert_payload(self) -> dict: ...  # type: ignore[empty-body]
        @property
        def org_environment(self) -> Severity:
            return Severity.DEV
        @property
        def trace_id(self) -> str:
            return "t"

    with pytest.raises(NotImplementedError):
        await _Ctx().docker_restart("c")
