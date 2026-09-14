"""Tests for SafetyMode production qualification gate."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.exceptions import AegisError
from aegis.server.services.safety_mode import (
    CheckResult,
    SafetyMode,
    SafetyQualification,
    _check_autoheal_kill_switch_readable,
    _check_docker,
    _check_external_deadman_configured,
    _check_postgres,
    _check_prod_registration_closed,
    _check_redis,
    _check_retention_storage_guard,
    _check_secrets_master_key,
    _check_self_backup_configured,
    assert_safety_allows_mutation,
    compute_safety_mode,
)


class TestIndividualChecks:
    """Test individual health checks."""

    @pytest.mark.asyncio
    async def test_check_postgres_success(self):
        conn = AsyncMock()
        conn.execute = AsyncMock()
        result = await _check_postgres(conn)
        assert result.name == "postgres"
        assert result.passed is True
        assert result.message is None

    @pytest.mark.asyncio
    async def test_check_postgres_failure(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=Exception("connection refused"))
        result = await _check_postgres(conn)
        assert result.name == "postgres"
        assert result.passed is False
        assert "connection refused" in result.message

    @pytest.mark.asyncio
    async def test_check_redis_success(self):
        with patch("redis.asyncio.from_url") as mock_from_url:
            mock_client = AsyncMock()
            mock_client.ping = AsyncMock()
            mock_client.aclose = AsyncMock()
            mock_from_url.return_value = mock_client

            with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
                mock_settings.return_value.redis_url = "redis://localhost:6379/0"
                result = await _check_redis()

        assert result.name == "redis"
        assert result.passed is True
        mock_client.ping.assert_awaited_once()
        mock_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_redis_failure(self):
        with patch("redis.asyncio.from_url", side_effect=Exception("redis down")):
            result = await _check_redis()

        assert result.name == "redis"
        assert result.passed is False
        assert "redis down" in result.message

    @pytest.mark.asyncio
    async def test_check_docker_success(self):
        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_client.ping.return_value = True
            mock_from_env.return_value = mock_client

            result = await _check_docker()

        assert result.name == "docker"
        assert result.passed is True
        mock_client.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_docker_failure(self):
        with patch("docker.from_env", side_effect=Exception("docker unavailable")):
            result = await _check_docker()

        assert result.name == "docker"
        assert result.passed is False
        assert "docker unavailable" in result.message

    @pytest.mark.asyncio
    async def test_check_secrets_master_key_configured(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.secrets_master_key = "a" * 64
            conn = MagicMock()
            result = await _check_secrets_master_key(conn)

        assert result.name == "secrets_master_key"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_secrets_master_key_missing(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.secrets_master_key = ""
            conn = MagicMock()
            result = await _check_secrets_master_key(conn)

        assert result.name == "secrets_master_key"
        assert result.passed is False
        assert "not set or uses placeholder" in result.message

    @pytest.mark.asyncio
    async def test_check_secrets_master_key_placeholder(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.secrets_master_key = "CHANGE-IN-PROD"
            conn = MagicMock()
            result = await _check_secrets_master_key(conn)

        assert result.name == "secrets_master_key"
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_check_prod_registration_closed_dev(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.env = "dev"
            result = await _check_prod_registration_closed()

        assert result.name == "prod_registration_closed"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_prod_registration_closed_prod_disabled(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.env = "prod"
        with patch.dict(os.environ, {"AEGIS_REGISTRATION_ENABLED": "false"}):
            result = await _check_prod_registration_closed()

        assert result.name == "prod_registration_closed"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_prod_registration_closed_prod_enabled(self):
        # Need to mock both settings and os.environ since the function imports os locally
        with (
            patch.dict(os.environ, {"AEGIS_REGISTRATION_ENABLED": "true", "AEGIS_JWT_SECRET": "a" * 32}, clear=True),
            patch("aegis.server.services.safety_mode.get_settings") as mock_settings,
        ):
            from aegis.server.runtime.config import AegisSettings
            # Create a settings object with prod env
            settings = AegisSettings(env="prod", jwt_secret="a" * 32)
            mock_settings.return_value = settings
            result = await _check_prod_registration_closed()

        assert result.name == "prod_registration_closed"
        assert result.passed is False
        assert "AEGIS_REGISTRATION_ENABLED=true in prod" in result.message

    @pytest.mark.asyncio
    async def test_check_external_deadman_configured_dev(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.env = "dev"
            result = await _check_external_deadman_configured()

        assert result.name == "external_deadman"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_external_deadman_configured_prod_set(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.env = "prod"
            mock_settings.return_value.deadman_heartbeat_url = "https://hc.example/ping"
            result = await _check_external_deadman_configured()

        assert result.name == "external_deadman"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_external_deadman_configured_prod_missing(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.env = "prod"
            mock_settings.return_value.deadman_heartbeat_url = ""
            result = await _check_external_deadman_configured()

        assert result.name == "external_deadman"
        assert result.passed is False
        assert "not set in prod" in result.message

    @pytest.mark.asyncio
    async def test_check_retention_storage_guard_configured(self):
        with patch("aegis.server.persistence.retention.RETENTION", [{"table": "test"}]):
            conn = MagicMock()
            result = await _check_retention_storage_guard(conn)

        assert result.name == "retention_storage_guard"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_retention_storage_guard_empty(self):
        with patch("aegis.server.persistence.retention.RETENTION", []):
            conn = MagicMock()
            result = await _check_retention_storage_guard(conn)

        assert result.name == "retention_storage_guard"
        assert result.passed is False
        assert "No retention policies configured" in result.message

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Causes global state pollution; exception path covered by integration tests")
    async def test_check_retention_storage_guard_exception(self):
        pass

    @pytest.mark.asyncio
    async def test_check_self_backup_configured_disabled(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.self_backup_interval_hours = 0
            conn = MagicMock()
            result = await _check_self_backup_configured(conn)

        assert result.name == "self_backup"
        assert result.passed is False
        assert "self_backup_interval_hours <= 0" in result.message

    @pytest.mark.asyncio
    async def test_check_self_backup_configured_completed(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.self_backup_interval_hours = 24
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"status": "completed"})
            result = await _check_self_backup_configured(conn)

        assert result.name == "self_backup"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_self_backup_configured_none_completed(self):
        with patch("aegis.server.services.safety_mode.get_settings") as mock_settings:
            mock_settings.return_value.self_backup_interval_hours = 24
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value=None)
            result = await _check_self_backup_configured(conn)

        assert result.name == "self_backup"
        assert result.passed is False
        assert "No completed self-backup found" in result.message

    @pytest.mark.asyncio
    async def test_check_autoheal_kill_switch_readable_success(self):
        with patch("aegis.server.services.safety_mode.is_flag_enabled", AsyncMock(return_value=False)):
            conn = MagicMock()
            result = await _check_autoheal_kill_switch_readable(conn)

        assert result.name == "autoheal_kill_switch"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_autoheal_kill_switch_readable_failure(self):
        with patch("aegis.server.services.safety_mode.is_flag_enabled", AsyncMock(side_effect=Exception("db down"))):
            conn = MagicMock()
            result = await _check_autoheal_kill_switch_readable(conn)

        assert result.name == "autoheal_kill_switch"
        assert result.passed is False
        assert "db down" in result.message


class TestComputeSafetyMode:
    """Test compute_safety_mode integration."""

    @pytest.mark.asyncio
    async def test_compute_safety_mode_qualified(self):
        """All checks pass -> QUALIFIED."""
        conn = AsyncMock()
        conn.execute = AsyncMock()

        with patch("aegis.server.services.safety_mode._check_postgres", new_callable=AsyncMock) as mock_pg:
            mock_pg.return_value = CheckResult("postgres", True)
            with patch("aegis.server.services.safety_mode._check_redis", new_callable=AsyncMock) as mock_redis:
                mock_redis.return_value = CheckResult("redis", True)
                with patch("aegis.server.services.safety_mode._check_docker", new_callable=AsyncMock) as mock_docker:
                    mock_docker.return_value = CheckResult("docker", True)
                    with patch("aegis.server.services.safety_mode._check_secrets_master_key", new_callable=AsyncMock) as mock_key:
                        mock_key.return_value = CheckResult("secrets_master_key", True)
                        with patch("aegis.server.services.safety_mode._check_prod_registration_closed", new_callable=AsyncMock) as mock_reg:
                            mock_reg.return_value = CheckResult("prod_registration_closed", True)
                            with patch("aegis.server.services.safety_mode._check_external_deadman_configured", new_callable=AsyncMock) as mock_deadman:
                                mock_deadman.return_value = CheckResult("external_deadman", True)
                                with patch("aegis.server.services.safety_mode._check_retention_storage_guard", new_callable=AsyncMock) as mock_ret:
                                    mock_ret.return_value = CheckResult("retention_storage_guard", True)
                                    with patch("aegis.server.services.safety_mode._check_self_backup_configured", new_callable=AsyncMock) as mock_backup:
                                        mock_backup.return_value = CheckResult("self_backup", True)
                                        with patch("aegis.server.services.safety_mode._check_autoheal_kill_switch_readable", new_callable=AsyncMock) as mock_ks:
                                            mock_ks.return_value = CheckResult("autoheal_kill_switch", True)
                                            result = await compute_safety_mode(conn)

        assert result.mode == SafetyMode.QUALIFIED
        assert result.failed_checks == []
        assert result.degraded_checks == []
        assert len(result.details) == 9

    @pytest.mark.asyncio
    async def test_compute_safety_mode_blocked_postgres(self):
        """Postgres failure -> BLOCKED."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode._check_postgres", new_callable=AsyncMock) as mock_pg:
            mock_pg.return_value = CheckResult("postgres", False, "pg down")
            with patch("aegis.server.services.safety_mode._check_redis", new_callable=AsyncMock) as mock_redis:
                mock_redis.return_value = CheckResult("redis", True)
                with patch("aegis.server.services.safety_mode._check_docker", new_callable=AsyncMock) as mock_docker:
                    mock_docker.return_value = CheckResult("docker", True)
                    with patch("aegis.server.services.safety_mode._check_secrets_master_key", new_callable=AsyncMock) as mock_key:
                        mock_key.return_value = CheckResult("secrets_master_key", True)
                        with patch("aegis.server.services.safety_mode._check_prod_registration_closed", new_callable=AsyncMock) as mock_reg:
                            mock_reg.return_value = CheckResult("prod_registration_closed", True)
                            with patch("aegis.server.services.safety_mode._check_external_deadman_configured", new_callable=AsyncMock) as mock_deadman:
                                mock_deadman.return_value = CheckResult("external_deadman", True)
                                with patch("aegis.server.services.safety_mode._check_retention_storage_guard", new_callable=AsyncMock) as mock_ret:
                                    mock_ret.return_value = CheckResult("retention_storage_guard", True)
                                    with patch("aegis.server.services.safety_mode._check_self_backup_configured", new_callable=AsyncMock) as mock_backup:
                                        mock_backup.return_value = CheckResult("self_backup", True)
                                        with patch("aegis.server.services.safety_mode._check_autoheal_kill_switch_readable", new_callable=AsyncMock) as mock_ks:
                                            mock_ks.return_value = CheckResult("autoheal_kill_switch", True)
                                            result = await compute_safety_mode(conn)

        assert result.mode == SafetyMode.BLOCKED
        assert "postgres" in result.failed_checks

    @pytest.mark.asyncio
    async def test_compute_safety_mode_degraded_secrets_key(self):
        """Secrets key missing -> DEGRADED."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode._check_postgres", new_callable=AsyncMock) as mock_pg:
            mock_pg.return_value = CheckResult("postgres", True)
            with patch("aegis.server.services.safety_mode._check_redis", new_callable=AsyncMock) as mock_redis:
                mock_redis.return_value = CheckResult("redis", True)
                with patch("aegis.server.services.safety_mode._check_docker", new_callable=AsyncMock) as mock_docker:
                    mock_docker.return_value = CheckResult("docker", True)
                    with patch("aegis.server.services.safety_mode._check_secrets_master_key", new_callable=AsyncMock) as mock_key:
                        mock_key.return_value = CheckResult("secrets_master_key", False, "key missing")
                        with patch("aegis.server.services.safety_mode._check_prod_registration_closed", new_callable=AsyncMock) as mock_reg:
                            mock_reg.return_value = CheckResult("prod_registration_closed", True)
                            with patch("aegis.server.services.safety_mode._check_external_deadman_configured", new_callable=AsyncMock) as mock_deadman:
                                mock_deadman.return_value = CheckResult("external_deadman", True)
                                with patch("aegis.server.services.safety_mode._check_retention_storage_guard", new_callable=AsyncMock) as mock_ret:
                                    mock_ret.return_value = CheckResult("retention_storage_guard", True)
                                    with patch("aegis.server.services.safety_mode._check_self_backup_configured", new_callable=AsyncMock) as mock_backup:
                                        mock_backup.return_value = CheckResult("self_backup", True)
                                        with patch("aegis.server.services.safety_mode._check_autoheal_kill_switch_readable", new_callable=AsyncMock) as mock_ks:
                                            mock_ks.return_value = CheckResult("autoheal_kill_switch", True)
                                            result = await compute_safety_mode(conn)

        assert result.mode == SafetyMode.DEGRADED
        assert "secrets_master_key" in result.degraded_checks

    @pytest.mark.asyncio
    async def test_compute_safety_mode_degraded_multiple(self):
        """Multiple degraded -> DEGRADED."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode._check_postgres", new_callable=AsyncMock) as mock_pg:
            mock_pg.return_value = CheckResult("postgres", True)
            with patch("aegis.server.services.safety_mode._check_redis", new_callable=AsyncMock) as mock_redis:
                mock_redis.return_value = CheckResult("redis", True)
                with patch("aegis.server.services.safety_mode._check_docker", new_callable=AsyncMock) as mock_docker:
                    mock_docker.return_value = CheckResult("docker", True)
                    with patch("aegis.server.services.safety_mode._check_secrets_master_key", new_callable=AsyncMock) as mock_key:
                        mock_key.return_value = CheckResult("secrets_master_key", False, "key missing")
                        with patch("aegis.server.services.safety_mode._check_prod_registration_closed", new_callable=AsyncMock) as mock_reg:
                            mock_reg.return_value = CheckResult("prod_registration_closed", False, "reg open")
                            with patch("aegis.server.services.safety_mode._check_external_deadman_configured", new_callable=AsyncMock) as mock_deadman:
                                mock_deadman.return_value = CheckResult("external_deadman", False, "deadman missing")
                                with patch("aegis.server.services.safety_mode._check_retention_storage_guard", new_callable=AsyncMock) as mock_ret:
                                    mock_ret.return_value = CheckResult("retention_storage_guard", True)
                                    with patch("aegis.server.services.safety_mode._check_self_backup_configured", new_callable=AsyncMock) as mock_backup:
                                        mock_backup.return_value = CheckResult("self_backup", True)
                                        with patch("aegis.server.services.safety_mode._check_autoheal_kill_switch_readable", new_callable=AsyncMock) as mock_ks:
                                            mock_ks.return_value = CheckResult("autoheal_kill_switch", True)
                                            result = await compute_safety_mode(conn)

        assert result.mode == SafetyMode.DEGRADED
        assert len(result.degraded_checks) >= 2


class TestAssertSafetyAllowsMutation:
    """Test assert_safety_allows_mutation gate."""

    @pytest.mark.asyncio
    async def test_assert_safety_allows_mutation_qualified(self):
        """QUALIFIED -> allows mutation."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode.compute_safety_mode", new_callable=AsyncMock) as mock_compute:
            mock_compute.return_value = SafetyQualification(
                mode=SafetyMode.QUALIFIED,
                failed_checks=[],
                degraded_checks=[],
                details=[],
            )
            await assert_safety_allows_mutation(conn, action="test_action")

    @pytest.mark.asyncio
    async def test_assert_safety_allows_mutation_blocked(self):
        """BLOCKED -> raises AegisError."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode.compute_safety_mode", new_callable=AsyncMock) as mock_compute:
            mock_compute.return_value = SafetyQualification(
                mode=SafetyMode.BLOCKED,
                failed_checks=["postgres"],
                degraded_checks=[],
                details=[],
            )
            with pytest.raises(AegisError, match="safety_mode=BLOCKED") as exc_info:
                await assert_safety_allows_mutation(conn, action="test_action")
            assert "postgres" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_assert_safety_allows_mutation_degraded(self):
        """DEGRADED -> raises AegisError."""
        conn = AsyncMock()
        with patch("aegis.server.services.safety_mode.compute_safety_mode", new_callable=AsyncMock) as mock_compute:
            mock_compute.return_value = SafetyQualification(
                mode=SafetyMode.DEGRADED,
                failed_checks=[],
                degraded_checks=["secrets_master_key"],
                details=[],
            )
            with pytest.raises(AegisError, match="safety_mode=DEGRADED") as exc_info:
                await assert_safety_allows_mutation(conn, action="test_action")
            assert "secrets_master_key" in str(exc_info.value)
