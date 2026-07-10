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

    # === Ollama 网关(§5.2 单卡多项目共享)===
    # 单张 GPU 被多个独立项目共享,各自直连会产生 NVML 抢占错误(2026-07-05 ocr-vllm
    # 崩溃事故实证)。aegis 接管为唯一网关:其它项目 MUST 经此转发,由并发闸门serialize
    # 对底层 Ollama 的实际调用,而非各自独立进程直接抢卡。
    ollama_gateway_max_concurrency: int = Field(
        default=1, description="同时穿透到 Ollama 的请求数上限(单卡默认串行=1)"
    )
    ollama_gateway_queue_timeout_sec: float = Field(
        default=60.0, description="排队等待并发闸门的最长秒数,超时返回 503(GPU 繁忙)"
    )
    ollama_gateway_token: str = Field(
        default="", description="调用网关所需的共享密钥(Bearer);空=不校验(仅限内网场景)"
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

    # §5.3 自愈安全层:抖动检测(同一目标 window 内自愈≥threshold 且仍异常 → 停手升级人工)
    # + 全局限流(单位窗口内真实动作上限,防连锁误伤)。窗口/阈值可注入覆盖(业务知识不焊库)。
    autoheal_flap_window_seconds: int = 1800  # 30 min
    autoheal_flap_threshold: int = 2
    autoheal_rate_limit_max: int = 10  # 每 window 最多真实自愈动作数
    autoheal_rate_limit_window_seconds: int = 3600  # 1 h

    # §3.2 告警抑制:父级 host 下线时抑制其子告警(消除单根因引发的告警风暴,只留 host-down 根因)。
    # host 存活由 alert_host_liveness_metric 的每 host 最新值判定(>0=up,否则 down)。空/无该指标 → 不抑制。
    alert_suppress_enabled: bool = True
    alert_host_liveness_metric: str = "node_up"

    # §9/§3.3 变更冻结窗口:高风险时段(如交易剧烈时段)MUST 禁自动自愈与部署。窗口活跃由
    # window_active_check 判定。change_freeze_start 空或 duration<=0 → 禁用(不冻结)。
    change_freeze_start: str = Field(default="", description="冻结窗起点 ISO 时刻;空=禁用")
    change_freeze_duration_seconds: int = Field(default=0)
    change_freeze_recurrence: str = Field(default="none", description="none|daily|weekly")
    change_freeze_weekdays: str = Field(
        default="", description="weekly 生效星期逗号列(0=周一..6=周日)"
    )

    # §10/§3.7 config-as-code 对账:周期比对声明态(installed_apps.image)与运行态(容器镜像),
    # 漂移写 config.drift 一等 change 事件(§10.1)。docker 不可达 → 跳过(不崩循环)。
    compose_drift_enabled: bool = True

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
    pyroscope_url: str = Field(default="")
    kubeconfig: str = Field(default="", description="Path to a kubeconfig file for the K8s viewer")

    # WebDAV remote-backup target (Nextcloud / Synology / any WebDAV server).
    backup_webdav_url: str = Field(default="", description="Base WebDAV URL, e.g. https://host/dav")
    backup_webdav_user: str = Field(default="")
    backup_webdav_password: str = Field(default="")

    # §6 L1 外部死人开关:aegis 每 60s 向此 URL 发心跳;编排循环全健康才发,任一卡死则抑制
    # → 外部 watcher(healthchecks.io 等)超时告警("谁看门人")。空=外部死人禁用(§11 degraded)。
    deadman_heartbeat_url: str = Field(
        default="", description="External dead-man heartbeat URL (§6 L1)"
    )
    deadman_heartbeat_timeout_sec: float = Field(default=5.0)

    # §11.4 平台自备份:定时 pg_dump 自身控制面 DB(事件/策略/指标 可恢复是底线)。
    # backup_target 可注入(标注归档去向);S3(backup_s3_*)配了则工件另上传离宿主(本地 dump 不抗宿主故障)。
    self_backup_interval_hours: float = Field(default=24.0)
    self_backup_retain: int = Field(default=7, description="保留最近 N 份自备份工件")
    self_backup_target: str = Field(
        default="local", description="自备份归档去向标注(§11.4 ⑥ 可注入)"
    )

    # §5.2 磁盘回收(R2 破坏性):存储守卫越阈时,在 data_dir 自有子树内回收可再生文件。
    # allowlist 硬约束绝不越界系统路径。R2 → 默认 dry_run,运维显式关闭才真删。
    disk_cleanup_dry_run: bool = Field(
        default=True, description="§5.2 磁盘回收干跑(R2 默认只统计不删)"
    )
    disk_cleanup_log_age_days: int = Field(default=14, description="超此天数的日志文件计为可回收")

    @model_validator(mode="after")
    def resolve_backup_local_dir(self) -> AegisSettings:
        if self.backup_local_dir == _UNSET_PATH:
            self.backup_local_dir = self.data_dir / "backups"
        return self


@lru_cache
def get_settings() -> AegisSettings:
    """Return cached AegisSettings singleton. Call get_settings.cache_clear() in tests."""
    return AegisSettings()
