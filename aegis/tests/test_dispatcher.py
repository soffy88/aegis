"""Tests for OmodulDispatcher (7 tests per §2.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import omodul
import pytest

from aegis.server.dispatch.budget_tracker import BudgetTracker
from aegis.server.dispatch.dedup_cache import DedupCache
from aegis.server.dispatch.omodul_dispatcher import BudgetExceededError, OmodulDispatcher


def _make_dispatcher(
    dedup: DedupCache | None = None,
    budget: BudgetTracker | None = None,
    data_dir: str = "/tmp/aegis_test",
) -> OmodulDispatcher:
    if dedup is None:
        dedup = mock.AsyncMock(spec=DedupCache)
        dedup.get.return_value = None
    if budget is None:
        budget = mock.AsyncMock(spec=BudgetTracker)
        budget.deduct.return_value = True  # reservation succeeds
    return OmodulDispatcher(dedup, budget, data_dir=data_dir)


def _mock_omodul_result() -> dict[str, Any]:
    return {
        "findings": {"container_id": "abc123"},
        "fingerprint": "fp_abc",
        "decision_trail": {"steps": [{"action": "pull_image"}]},
        "report_path": "/tmp/report.md",
        "cost_usd": 0.02,
        "status": "completed",
        "error": None,
    }


class _FakeConfig:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeInput:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _patch_omodul():
    """Patch omodul module with fake classes and functions for all dispatcher tests."""
    fake_fn = mock.MagicMock(return_value=_mock_omodul_result())

    with (
        mock.patch.object(omodul, "install_self_hosted_app", fake_fn, create=True),
        mock.patch.object(omodul, "InstallSelfHostedAppConfig", _FakeConfig, create=True),
        mock.patch.object(omodul, "InstallSelfHostedAppInput", _FakeInput, create=True),
        mock.patch.object(omodul, "compute_fingerprint_for", return_value="fp_abc", create=True),
        mock.patch(
            "aegis.server.persistence.event_trail.save_decision_trail",
            new_callable=mock.AsyncMock,
        ),
    ):
        yield fake_fn


@pytest.mark.asyncio
async def test_dispatcher_invokes_omodul_correctly(tmp_path: Path, _patch_omodul: Any) -> None:
    """Dispatcher calls omodul function with correct args."""
    dispatcher = _make_dispatcher(data_dir=str(tmp_path))

    r = await dispatcher.invoke(
        omodul_name="install_self_hosted_app",
        config={"app_slug": "nginx"},
        input_data={"app_config": {}},
        user_id="user_1",
    )

    assert r["status"] == "completed"
    _patch_omodul.assert_called_once()


@pytest.mark.asyncio
async def test_dispatcher_dedup_hit_skips_omodul(tmp_path: Path, _patch_omodul: Any) -> None:
    """Same fingerprint second time returns cached result without calling omodul."""
    dedup = mock.AsyncMock(spec=DedupCache)
    cached = _mock_omodul_result()
    dedup.get.return_value = cached

    dispatcher = _make_dispatcher(dedup=dedup, data_dir=str(tmp_path))

    r = await dispatcher.invoke(
        omodul_name="install_self_hosted_app",
        config={"app_slug": "nginx"},
        input_data={"app_config": {}},
        user_id="user_1",
    )

    assert r == cached
    _patch_omodul.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_budget_exceeded_raises(tmp_path: Path, _patch_omodul: Any) -> None:
    """Reservation (deduct) failing raises BudgetExceededError and never invokes omodul."""
    budget = mock.AsyncMock(spec=BudgetTracker)
    budget.deduct.return_value = False  # reservation rejected → over budget

    dispatcher = _make_dispatcher(budget=budget, data_dir=str(tmp_path))

    with pytest.raises(BudgetExceededError):
        await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={"app_slug": "nginx"},
            input_data={"app_config": {}},
            user_id="user_1",
        )
    _patch_omodul.assert_not_called()  # gated BEFORE spending money
    budget.settle.assert_not_awaited()  # nothing reserved → nothing to reconcile


@pytest.mark.asyncio
async def test_dispatcher_reserves_then_settles_actual_cost(
    tmp_path: Path, _patch_omodul: Any
) -> None:
    """Happy path reserves budget_usd up front, then settles to the real cost_usd."""
    budget = mock.AsyncMock(spec=BudgetTracker)
    budget.deduct.return_value = True

    dispatcher = _make_dispatcher(budget=budget, data_dir=str(tmp_path))

    await dispatcher.invoke(
        omodul_name="install_self_hosted_app",
        config={"app_slug": "nginx", "budget_usd": 5.0},
        input_data={"app_config": {}},
        user_id="user_1",
    )

    budget.deduct.assert_awaited_once_with("user_1", 5.0)  # reserved the cap
    budget.settle.assert_awaited_once_with(
        "user_1", reserved_usd=5.0, actual_usd=0.02
    )  # reconciled to real spend (_mock_omodul_result cost_usd)


@pytest.mark.asyncio
async def test_dispatcher_settles_zero_on_failure(tmp_path: Path) -> None:
    """A failed omodul run refunds the whole reservation (settle actual=0)."""
    budget = mock.AsyncMock(spec=BudgetTracker)
    budget.deduct.return_value = True
    fake_fn = mock.MagicMock(side_effect=RuntimeError("boom"))

    with (
        mock.patch.object(omodul, "install_self_hosted_app", fake_fn, create=True),
        mock.patch.object(omodul, "InstallSelfHostedAppConfig", _FakeConfig, create=True),
        mock.patch.object(omodul, "InstallSelfHostedAppInput", _FakeInput, create=True),
        mock.patch.object(omodul, "compute_fingerprint_for", return_value="fp_x", create=True),
    ):
        dispatcher = _make_dispatcher(budget=budget, data_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            await dispatcher.invoke(
                omodul_name="install_self_hosted_app",
                config={"app_slug": "nginx", "budget_usd": 5.0},
                input_data={"app_config": {}},
                user_id="user_1",
            )

    budget.settle.assert_awaited_once_with("user_1", reserved_usd=5.0, actual_usd=0.0)


@pytest.mark.asyncio
async def test_dispatcher_does_not_recompute_fingerprint(tmp_path: Path) -> None:
    """Dispatcher calls omodul.compute_fingerprint_for, never self-computes."""
    dispatcher = _make_dispatcher(data_dir=str(tmp_path))

    with mock.patch.object(omodul, "compute_fingerprint_for", return_value="fp_abc") as mock_fp:
        await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={"app_slug": "nginx"},
            input_data={"app_config": {}},
            user_id="user_1",
        )

    mock_fp.assert_called_once()
    args = mock_fp.call_args[0]
    assert args[0] == "install_self_hosted_app"


@pytest.mark.asyncio
async def test_dispatcher_output_dir_contains_user_id(tmp_path: Path) -> None:
    """output_dir path contains user_id."""
    dispatcher = _make_dispatcher(data_dir=str(tmp_path))

    captured_args: list[Any] = []

    def capture_call(cfg: Any, inp: Any, out_dir: Path, **kw: Any) -> dict[str, Any]:
        captured_args.append(out_dir)
        return _mock_omodul_result()

    with mock.patch.object(omodul, "install_self_hosted_app", side_effect=capture_call):
        await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={"app_slug": "nginx"},
            input_data={"app_config": {}},
            user_id="user_42",
        )

    assert "user_42" in str(captured_args[0])


@pytest.mark.asyncio
async def test_dispatcher_persists_decision_trail(tmp_path: Path) -> None:
    """After omodul returns, save_decision_trail is called."""
    dispatcher = _make_dispatcher(data_dir=str(tmp_path))

    with mock.patch(
        "aegis.server.persistence.event_trail.save_decision_trail",
        new_callable=mock.AsyncMock,
    ) as mock_save:
        await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={"app_slug": "nginx"},
            input_data={"app_config": {}},
            user_id="user_1",
        )

    mock_save.assert_called_once()
    kw = mock_save.call_args.kwargs
    assert kw["omodul_name"] == "install_self_hosted_app"
    assert kw["fingerprint"] == "fp_abc"
    assert kw["user_id"] == "user_1"
    assert kw["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatcher_omodul_failure_no_dedup_but_persists(tmp_path: Path) -> None:
    """omodul status=failed → no dedup cache write, but decision_trail still persisted."""
    dedup = mock.AsyncMock(spec=DedupCache)
    dedup.get.return_value = None
    dispatcher = _make_dispatcher(dedup=dedup, data_dir=str(tmp_path))

    failed_result = _mock_omodul_result()
    failed_result["status"] = "failed"
    failed_result["error"] = {"msg": "container crash"}

    with (
        mock.patch.object(omodul, "install_self_hosted_app", return_value=failed_result),
        mock.patch(
            "aegis.server.persistence.event_trail.save_decision_trail",
            new_callable=mock.AsyncMock,
        ) as mock_save,
    ):
        r = await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={"app_slug": "nginx"},
            input_data={"app_config": {}},
            user_id="user_1",
        )

    assert r["status"] == "failed"
    dedup.set.assert_not_called()
    mock_save.assert_called_once()


@pytest.mark.xfail(
    reason="主库 omodul __version__ 缺失 (Wiki 已知, 待主库 PATCH bump 修复). "
    "Aegis 测试此处显式标记为 xfail, 不阻塞 PR. 主库修复后此测试转 xpass.",
    strict=False,
)
def test_omodul_exposes_version() -> None:
    """MF4: omodul should expose __version__ (Step 12 SPEC §1.4.2)."""
    assert hasattr(omodul, "__version__"), (
        "omodul 缺 __version__ 属性; 见 Wiki 2026-05-24 反馈, 主库 PATCH bump 时修."
    )
    assert omodul.__version__  # non-empty
