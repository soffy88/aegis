"""Tests for the shared SSRF host-allowlist guard used by remediation plugins."""

from __future__ import annotations

import pytest
from aegis_plugins._url_safety import UrlNotAllowed, check_url_allowed


def test_rejects_when_allowlist_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", raising=False)
    with pytest.raises(UrlNotAllowed):
        check_url_allowed("http://svc/reload")


def test_rejects_when_allowlist_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "")
    with pytest.raises(UrlNotAllowed):
        check_url_allowed("http://svc/reload")


def test_rejects_host_not_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "svc")
    with pytest.raises(UrlNotAllowed):
        check_url_allowed("http://attacker.example/reload")


def test_allows_host_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "svc")
    check_url_allowed("http://svc:8080/reload")  # must not raise


def test_allows_colon_separated_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "svc:dep")
    check_url_allowed("http://dep/health")  # must not raise


def test_host_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_REMEDIATION_ALLOWED_HOSTS", "SVC")
    check_url_allowed("http://svc/reload")  # must not raise
