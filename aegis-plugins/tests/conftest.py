"""Shared pytest fixtures for aegis-plugins tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_url_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the SSRF host allowlist with the fixture hostnames used in tests.

    ``aegis_plugins._url_safety.check_url_allowed`` fails closed when
    ``AEGIS_REMEDIATION_ALLOWED_HOSTS`` is unset, so plugin tests that exercise a
    real ``ctx.http_get`` call need it populated. Tests that specifically probe the
    guard's reject behavior (see test_url_safety.py) override/clear it themselves.
    """
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "docker,vault,svc,dep")
