"""Server config — loaded from env + defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator
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
        default="postgresql://aegis:aegis@localhost:5434/aegis",
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

    # === File Manager (host filesystem browser) ===
    # Whitelist of host directories the file manager may browse/operate on.
    # Empty list disables the feature entirely. Each root must also be
    # bind-mounted into the aegis-backend container at the same path.
    file_manager_roots: Any = Field(
        default_factory=list,
        description=(
            "Colon- or comma-separated host dirs the file manager may access. "
            "Empty disables the feature. env: AEGIS_FILE_MANAGER_ROOTS"
        ),
        validation_alias=AliasChoices("AEGIS_FILE_MANAGER_ROOTS"),
    )

    @field_validator("file_manager_roots", mode="before")
    @classmethod
    def parse_file_manager_roots(cls, v: object) -> object:
        if isinstance(v, str):
            parts = [p.strip() for chunk in v.split(":") for p in chunk.split(",")]
            return [Path(p) for p in parts if p]
        if isinstance(v, list):
            return [Path(p) for p in v]
        return v

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
    ollama_base_url: str | None = Field(
        default=None, description="Ollama API base URL (AEGIS_DESIGN 决策 2 双轨, 可选)"
    )

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
    # Edge/ingress controller URL used by the domains router to provision routes.
    domain_edge_url: str = "http://localhost:8081"
    # Docker network to attach app-store installs to (so caddy/monitoring can reach
    # them). Empty = default bridge with published ports.
    app_install_network: str = ""
    caddy_config_dir: Path = Field(
        default=_UNSET_PATH,
        description="Caddy config dir (default: data_dir/caddy). Override: AEGIS_CADDY_CONFIG_DIR",
    )

    @model_validator(mode="after")
    def resolve_caddy_config_dir(self) -> AegisSettings:
        if self.caddy_config_dir == _UNSET_PATH:
            self.caddy_config_dir = self.data_dir / "caddy"
        return self

    # === Logging ===
    log_level: str = "INFO"
    log_format: str = Field(
        default="text",
        description="Log output format: 'text' (human) or 'json' (aggregation). env: AEGIS_LOG_FORMAT",
    )

    # === JWT ===
    jwt_secret: str = Field(
        default="dev-secret-CHANGE-IN-PROD-MUST-BE-32-BYTES",
        min_length=32,
        description="HS256 signing secret, env: AEGIS_JWT_SECRET",
    )
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60  # 1 hour
    jwt_refresh_ttl_days: int = 30  # 30 days
    jwt_refresh_secure: bool = Field(
        default=True,
        description=(
            "Set Secure flag on refresh cookie. Default True (HTTPS). "
            "Set AEGIS_JWT_REFRESH_SECURE=false for local HTTP-only dev."
        ),
    )

    # === Password policy (M1 relaxed, M2 tighten) ===
    password_min_length: int = 8

    # === Secrets vault ===
    secrets_master_key: str = Field(
        default="",
        description=(
            "Hex-encoded 32-byte master key for the encrypted secrets vault. If empty, "
            "derived from jwt_secret (dev convenience). Set a dedicated "
            "AEGIS_SECRETS_MASTER_KEY in prod so rotating the JWT secret doesn't orphan "
            "stored secrets."
        ),
    )

    # === CORS ===
    cors_allowed_origins: Any = Field(
        default_factory=lambda: ["http://localhost:3010"],
        description="允许的 CORS origin. env: AEGIS_CORS_ALLOWED_ORIGINS",
        validation_alias=AliasChoices("AEGIS_CORS_ALLOWED_ORIGINS", "AEGIS_CORS_ORIGINS"),
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # === Platform Alerter (S1 BrainAlerter) ===
    platform_alerter_interval_seconds: int = 60
    platform_alerter_throttle_seconds: int = 600
    platform_alerter_thresholds: dict = Field(
        default_factory=lambda: {
            "cpu_percent": 85.0,
            "ram_percent": 90.0,
            "disk_percent": 85.0,
            "pool_usage_percent": 85.0,
            "slow_query_ms": 5000,
            "rabbitmq_queue_depth": 1000,
            "rabbitmq_consumer_min": 1,
        }
    )
    platform_alerter_telegram_bot_token: str = ""
    platform_alerter_telegram_chat_id: str = ""
    platform_alerter_rabbitmq_mgmt_url: str = ""
    platform_alerter_rabbitmq_queue_name: str = "tasks"
    platform_alerter_disk_path: str = "/"

    # === Brain / RCA (S1) ===
    rca_llm_model: str = "claude-sonnet-4-6"
    rca_max_steps: int = 10
    rca_max_cost_usd_per_invocation: float = 5.0
    # Per-org daily RCA spend ceiling, enforced against actual USD spend summed
    # from llm_cost_ledger over the trailing day. 0 disables the daily gate.
    rca_max_cost_usd_per_org_daily: float = 25.0
    # When the budget check itself errors (DB/infra), allow the RCA (True) or block
    # it (False). Default True: incident response must not be blocked by a budget
    # -infra outage, and the per-invocation cap still bounds single-call cost. Set
    # False to prioritise cost-safety over availability.
    rca_budget_fail_open: bool = True
    planner_llm_model: str = "claude-sonnet-4-6"
    triage_llm_model: str = "claude-haiku-4-5"
    triage_max_tokens: int = 1024
    triage_throttle_seconds: int = 60

    # === Vector Store / RAG (BACKLOG-073) ===
    runbook_vector_db_path: Path = Field(
        default=_UNSET_PATH,
        description="LanceDB runbook vector store 路径 (默认: data_dir/vector/runbooks)",
    )
    runbook_vector_dim: int = Field(
        default=1024,
        description=(
            "Embedding 维度，需与 embedding provider 匹配 "
            "(bge-m3=1024, text-embedding-3-small=1536)"
        ),
    )
    runbook_vector_collection: str = "runbooks"
    runbook_top_k: int = 5
    runbook_min_score: float = 0.5
    embedding_provider: str = Field(
        default="default",
        description="obase ProviderRegistry 中注册的 embedding provider 名",
    )

    # === Rate limiting ===
    rate_limit_auth_requests: int = Field(
        default=10,
        description="Max login/register attempts per IP per window (AEGIS_RATE_LIMIT_AUTH_REQUESTS)",
    )
    rate_limit_auth_window_sec: int = Field(
        default=60,
        description="Sliding window in seconds for auth rate limit (AEGIS_RATE_LIMIT_AUTH_WINDOW_SEC)",
    )

    # === Agent (S3) ===
    agent_token: str = Field(
        default="",
        description="Bearer token required from aegis-agent. Empty = no auth (dev).",
    )
    agent_metrics_retention_days: int = Field(
        default=30,
        ge=0,
        description="Prune agent_metrics rows older than this many days. 0 disables pruning.",
    )

    # === Capacity forecaster (cron) ===
    capacity_min_samples: int = Field(
        default=4, ge=2, description="Minimum samples before a metric is forecast."
    )
    capacity_default_threshold: float = Field(
        default=90.0, description="Breach threshold (%) for metrics without a specific override."
    )
    capacity_breach_days_warn: int = Field(
        default=30, ge=1, description="Forecast horizon (steps/days) for breach warnings."
    )
    capacity_metric_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "disk_usage_percent": 90.0,
            "ram_usage_percent": 95.0,
            "cpu_usage_percent": 95.0,
            "db_connection_pool_used": 90.0,
        },
        description="Per-metric breach thresholds (%). JSON env override supported.",
    )

    # === AppStore (S2) ===
    appstore_catalog_url: str = ""
    appstore_health_retries: int = 5
    appstore_skip_pull: bool = False

    # === AutoHeal Engine (S2) ===
    autoheal_circuit_breaker_enabled: bool = True
    autoheal_diagnose_min_confidence: float = 0.5
    autoheal_health_retries: int = 5

    # === Email (Resend) ===
    resend_api_key: str = Field(
        default="",
        description="Resend API key for transactional email. Empty = log only (dev).",
    )
    email_from_addr: str = Field(
        default="noreply@aegis.uex.hk",
        description="From address for Aegis-sent emails (env: AEGIS_EMAIL_FROM_ADDR)",
    )

    # === Environment ===
    env: str = Field(
        default="dev",
        description="Runtime environment: dev / prod (env: AEGIS_ENV)",
    )

    @model_validator(mode="after")
    def validate_jwt_secret_in_prod(self) -> AegisSettings:
        # Fail closed: reject the placeholder secret unless env is explicitly "dev".
        # If AEGIS_ENV is unset, the default "dev" is still acceptable for local dev.
        # The key condition: if someone sets ENV=prod (docker-compose does this) but
        # forgets AEGIS_JWT_SECRET, we must refuse to start.
        is_dev = self.env == "dev"
        has_placeholder = "CHANGE-IN-PROD" in self.jwt_secret
        if has_placeholder and not is_dev:
            raise ValueError(
                "AEGIS_JWT_SECRET must be set to a strong secret when AEGIS_ENV != 'dev'. "
                "Generate one with: openssl rand -hex 32"
            )
        return self

    @model_validator(mode="after")
    def resolve_runbook_vector_db_path(self) -> AegisSettings:
        if self.runbook_vector_db_path == _UNSET_PATH:
            self.runbook_vector_db_path = self.data_dir / "vector" / "runbooks"
        return self

    # === Backup / S3 ===
    backup_s3_bucket: str = Field(
        default="", description="S3 bucket for backups. Empty = local only"
    )
    backup_s3_endpoint_url: str | None = Field(
        default=None, description="Custom S3 endpoint (MinIO etc)"
    )
    backup_s3_access_key_id: str = Field(default="", alias="AWS_ACCESS_KEY_ID")
    backup_s3_secret_access_key: str = Field(default="", alias="AWS_SECRET_ACCESS_KEY")
    backup_s3_region: str = Field(default="us-east-1")
    backup_local_dir: Path = Field(
        default=_UNSET_PATH, description="Local backup dir (default: data_dir/backups)"
    )
    # Telemetry (OTLP trace ingest). If set, exporters must send this as the
    # X-Aegis-Ingest-Key header. Empty = open ingest (fine on a private network).
    telemetry_ingest_key: str = Field(default="")
    # Loki base URL for the log-query page (e.g. http://loki:3100). Empty = disabled.
    loki_url: str = Field(default="")

    # WebDAV remote-backup target (Nextcloud / Synology / any WebDAV server).
    backup_webdav_url: str = Field(default="", description="Base WebDAV URL, e.g. https://host/dav")
    backup_webdav_user: str = Field(default="")
    backup_webdav_password: str = Field(default="")

    @model_validator(mode="after")
    def resolve_backup_local_dir(self) -> AegisSettings:
        if self.backup_local_dir == _UNSET_PATH:
            self.backup_local_dir = self.data_dir / "backups"
        return self


@lru_cache
def get_settings() -> AegisSettings:
    """Return cached AegisSettings singleton. Call get_settings.cache_clear() in tests."""
    return AegisSettings()
