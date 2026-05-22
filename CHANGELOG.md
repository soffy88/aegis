# Changelog

## [0.2.4] — 2026-05-23

### Added
- `AEGIS_CORS_ORIGINS` env var for CORS configuration (comma-separated, default `http://localhost:3010`). Production deployments should set this to the actual console domain.

### Fixed
- Remove hardcoded `localhost:3000/3001` from CORSMiddleware; aegis-console dev port is 3010.

## [0.2.3] — 2026-05-22

### Fixed
- caddy_config_dir 默认值改为 data_dir/caddy (sentinel + validator),不再硬编码 /etc/caddy/aegis (BATCH 15 §3)

### Added
- AEGIS_CADDY_CONFIG_DIR 环境变量支持
- pre-commit hooks (codename guard + hardcoded-path guard + inline-secret guard + ruff)
- .github/workflows/ci.yml for aegis + aegis-console

## [0.2.2] — 2026-05-22

### Fixed
- PoC bug #5: install output_dir 改用 settings.data_dir,不再硬编码 /var/lib/aegis (BATCH 13, commit 69c87ef)
- PoC bug #6: install_dir 必填,后端 422 + 前端 zod required,移除 tempfile.mkdtemp fallback (BATCH 13+14)
- log_dir 默认值改为 data_dir/logs (sentinel + validator),不再硬编码 /var/log/aegis (BATCH 14, commit cdb8308)

### Added
- Console install 表单 install_dir 必填校验 + placeholder ~/apps/{slug} (BATCH 14, commit a03cfb3)
- Install 全流程 e2e 测试 (7 个, BATCH 14, commit 7318d97)
- AEGIS_DATA_DIR / AEGIS_LOG_DIR 环境变量支持

### Known issues (BATCH 15 backlog)
- caddy_config_dir 默认 /etc/caddy/aegis 普通用户无写权限 (跟 log_dir 同款,留 BATCH 15)
- Install e2e 测试用 mock 不走真容器,真容器 smoke test 留后续

## [0.2.0] — 2026-05-22

### Fixed (v0.7 PoC 撞出的 4 bug)
- **#1 self-hosted seed**: migration 004 自动 seed 默认 orgs/projects 行,
  消除首次启动 ForeignKeyViolationError.
- **#2-3 cross-repo deps**: pyproject.toml 显式列 `obase` / `oprim` 依赖,
  加 INSTALL.md 部署文档, 新增 `test_dependency_imports` smoke test.
- **#4 BackgroundTask silent fail**: 重写 `runtime/logging.py` 强制 root logger
  + 让 aegis/omodul/oskill/oprim 命名空间传播, 改 `_run_install`
  分别捕获 ImportError vs Exception, error_detail 写入 event_trail.

### Added
- `INSTALL.md` — 标准部署文档 (含 cross-repo install 顺序).
- migration `004_seed_self_hosted_defaults`.
- 11 个新测试 (seed migration 验证 / 依赖 import smoke / 异常捕获验证).

### Validated
- 2026-05-22 PoC: omodul.install_app 端到端真装 uptime-kuma 成功
  (容器 healthy, port 3011 HTTP 302).

## [0.1.0] — 2026-05-20

### Added — Server skeleton
- FastAPI application factory (`aegis.server.app.create_app`)
- Postgres async pool (`aegis.server.persistence.db`)
- Migrations runner with 3 initial migrations:
  - 001_event_trail (with causal chain indices)
  - 002_orgs_projects_users (multi-tenant tables)
  - 003_installed_apps (apps + domains)
- event_trail writer/reader API (append/recent/causal_chain)
- Brain pipeline skeleton (Triage → RCA → Runbook stub stages)
- AutoHealDispatcher skeleton (plugin matching + dry-run logging)
- Plugin loader via entry_points
- 3 routers: /health, /api/v1/events, /api/v1/alerts/ingest
- CLI: `aegis serve` / `aegis migrate`
- 5 plan quotas (free/indie/team/business/enterprise)
- Settings via env (pydantic-settings, AEGIS_ prefix)

### Not yet implemented
- Actual LLM calls in Brain (needs omodul.triage_alert / diagnose_root_cause /
  propose_runbook from 3O main repo)
- AutoHeal lifecycle execution (needs AutoHealContext concrete impl)
- Console / GraphQL (BATCH 10+)
- Docker manager / AppStore installer / Caddy config gen (BATCH 9+)

### Spec
- Aegis ARCHITECTURE v0.6 §5-§13
- Aegis ARCHITECTURE v0.7 增量
