"""Tests for settings + plan_quotas."""
from __future__ import annotations

import pytest

from aegis.server.runtime.config import AegisSettings
from aegis.server.runtime.plan_quotas import PLAN_QUOTAS, get_quota


class TestSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AEGIS_PORT", raising=False)
        s = AegisSettings()
        assert s.port == 8080
        assert s.self_hosted_mode is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_PORT", "9999")
        monkeypatch.setenv("AEGIS_LLM_MODEL_DEFAULT", "claude-opus-4-7")
        s = AegisSettings()
        assert s.port == 9999
        assert s.llm_model_default == "claude-opus-4-7"


class TestPlanQuotas:
    def test_all_5_plans_defined(self) -> None:
        assert set(PLAN_QUOTAS.keys()) == {"free", "indie", "team", "business", "enterprise"}

    def test_free_no_llm(self) -> None:
        assert get_quota("free", "llm_tokens_monthly") == 0

    def test_enterprise_unlimited(self) -> None:
        assert get_quota("enterprise", "events_monthly") == -1

    def test_unknown_plan_falls_back_to_free(self) -> None:
        assert get_quota("unknown_plan", "llm_tokens_monthly") == 0
