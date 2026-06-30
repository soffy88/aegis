"""Audit #22: warn when the secrets vault key is derived from jwt_secret."""

from __future__ import annotations

import logging

from aegis.server.services import secrets_vault
from aegis.server.runtime.config import AegisSettings


def test_warns_once_when_no_dedicated_master_key(caplog):
    secrets_vault._warned_derived_key = False  # reset the log-once flag
    cfg = AegisSettings(secrets_master_key="")
    with caplog.at_level(logging.WARNING):
        k1 = secrets_vault.master_key(cfg)
        secrets_vault.master_key(cfg)  # second call must NOT warn again
    assert len(k1) == 32
    warnings = [r for r in caplog.records if "secrets_master_key_derived" in r.message]
    assert len(warnings) == 1


def test_no_warning_with_dedicated_key(caplog):
    secrets_vault._warned_derived_key = False
    cfg = AegisSettings(secrets_master_key="00" * 32)  # 32-byte hex
    with caplog.at_level(logging.WARNING):
        k = secrets_vault.master_key(cfg)
    assert k == bytes(32)
    assert not any("secrets_master_key_derived" in r.message for r in caplog.records)
