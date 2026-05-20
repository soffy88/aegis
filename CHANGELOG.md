# Changelog

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
