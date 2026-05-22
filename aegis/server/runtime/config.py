"""Server config — loaded from env + defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_UNSET_PATH = Path("__aegis_unset__")


class AegisSettings(BaseSettings):
    """Aegis main server configuration.

    All settings come from env vars (prefix AEGIS_).
    """

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === Network ===
    host: str = "0.0.0.0"
    port: int = 8080

    # === Postgres ===
    postgres_dsn: str = Field(
        default="postgresql://aegis:aegis@localhost:5432/aegis",
        description="Postgres connection string (asyncpg-compatible)",
    )
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # === Redis ===
    redis_url: str = "redis://localhost:6379/0"

    # === Storage ===
    data_dir: Path = Field(
        default_factory=lambda: Path.home() / ".aegis",
        description="Root data directory; override with AEGIS_DATA_DIR",
    )
    log_dir: Path = Field(
        default=_UNSET_PATH,
        description="Log directory; defaults to data_dir/logs. Override with AEGIS_LOG_DIR",
    )

    @model_validator(mode="after")
    def resolve_log_dir(self) -> AegisSettings:
        if self.log_dir == _UNSET_PATH:
            self.log_dir = self.data_dir / "logs"
        return self

    # === Plan / quotas ===
    default_org_plan: str = Field(
        default="free",
        description="Plan assigned to new orgs; for self-hosted use 'enterprise' to unlock all",
    )
    self_hosted_mode: bool = Field(
        default=True,
        description="If true, auto-assign enterprise plan + skip billing",
    )

    # === LLM provider routing ===
    llm_provider: str = Field(default="anthropic", description="anthropic / openai / ollama")
    llm_model_default: str = "claude-haiku-4-5"
    llm_model_premium: str = "claude-sonnet-4-6"

    # === AutoHeal ===
    autoheal_enabled: bool = True
    autoheal_dry_run: bool = Field(
        default=True,
        description="If true, log actions but don't execute (safety default)",
    )

    # === Docker ===
    docker_host: str = "unix:///var/run/docker.sock"
    docker_socket_proxy_enabled: bool = True

    # === Caddy ===
    caddy_admin_url: str = "http://localhost:2019"
    caddy_config_dir: str = "/etc/caddy/aegis"

    # === Logging ===
    log_level: str = "INFO"
