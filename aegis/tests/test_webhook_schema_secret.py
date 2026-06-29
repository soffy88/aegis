"""Tests for webhook secret-reference validation — C2-5 (fail-closed in prod)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis.server.schemas.webhook import (
    WebhookSubscriptionCreate,
    WebhookSubscriptionUpdate,
)

_BASE = {"name": "x", "url": "https://example.com/hook", "event_types": ["alert.fired"]}


def _create(secret: str | None) -> WebhookSubscriptionCreate:
    return WebhookSubscriptionCreate(**_BASE, secret_encrypted=secret)


# ── dev: permissive ───────────────────────────────────────────────────────────


def test_dev_allows_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "dev")
    assert _create("plain:s").secret_encrypted == "plain:s"


def test_none_secret_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "prod")
    assert _create(None).secret_encrypted is None


def test_env_ref_with_allowlisted_prefix_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "prod")
    assert _create("env:AEGIS_WEBHOOK_SECRET_A").secret_encrypted == "env:AEGIS_WEBHOOK_SECRET_A"


# ── prod / scheme: fail closed ────────────────────────────────────────────────


def test_prod_rejects_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "prod")
    with pytest.raises(ValidationError):
        _create("plain:s")


def test_env_ref_outside_allowlist_prefix_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "dev")
    with pytest.raises(ValidationError):
        _create("env:SOME_OTHER_VAR")


def test_unknown_scheme_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown schemes would silently disable signing in the dispatcher → reject."""
    monkeypatch.setenv("AEGIS_ENV", "dev")
    with pytest.raises(ValidationError):
        _create("rawsecret")


def test_update_schema_also_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_ENV", "prod")
    with pytest.raises(ValidationError):
        WebhookSubscriptionUpdate(secret_encrypted="plain:s")
