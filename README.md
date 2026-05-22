# Aegis

Self-hosted ops platform for small SRE teams (1-20 people) and homelab tinkerers.
Think CasaOS + Datadog: one-click app install, container management, health monitoring,
AI-assisted runbooks — all on your own box, no SaaS lock-in.

**Status**: v0.2.2, PoC validated 2026-05-22. Self-use phase before v1.0.

## What's in

- **AppStore**: 278 apps, one-click install via Docker
- **Health monitoring**: container stats, healthchecks, event causal chain (`event_trail`)
- **AutoHeal**: plugin system with dry-run + human approval — **not** an AI agent
- **Caddy reverse proxy**: auto SSL, sane defaults
- **Console**: Next.js dashboard at port 3010 (separate repo: aegis-console)

## What's not

By design, Aegis does **not** do:
- Kubernetes / K8s-style orchestration
- Multi-cloud / cross-host scheduling
- GPU scheduling across projects
- Centralized dashboards spanning multiple machines
- Message buses (Kafka et al.)

If you need those, use the right tool. Aegis is for one box, small team.

## PoC validation (2026-05-22)

| Acceptance criterion | Result |
|---|---|
| One-click install uptime-kuma via Console | ✅ Pass |
| Container health surfaces in dashboard | ✅ Pass |
| event_trail captures install + lifecycle | ✅ Pass |
| AutoHeal dry-run executes plugin without side effect | ✅ Pass |

6 PoC bugs found, 6 fixed (BATCH 12-14). See [CHANGELOG.md](CHANGELOG.md) for details.

## Install

See [INSTALL.md](INSTALL.md).

## License & business model

- **Core**: Apache 2.0 (this repo)
- **Cloud SaaS**: managed hosting, planned post-v1.0
- **Enterprise**: paid features (RBAC, audit log, secret rotation, SSO), planned post-v1.0

The Apache core stays fully usable forever. The Cloud / Enterprise tiers add operational
conveniences, not feature lock-in.

## Status notes (honest version)

- **AI mode**: Aegis suggests, humans decide. No autonomous AI agent anywhere.
- **Security maturity**: pre-v0.8. No RBAC, no audit log, no secrets management.
  Single-user / single-tenant only at this stage.
- **Tested coverage**: backend 89%, console 3 tests (manual install flow only).
