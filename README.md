# Aegis

AI-powered self-hosted PaaS combining:
- AIOps (alerts / auto-heal / event causal chain)
- Docker management
- AppStore (278 apps)
- Caddy reverse proxy

Status: v0.1.0 server skeleton.

## Run

```bash
uvicorn aegis.server.app:app --reload --port 8080
curl http://localhost:8080/health
```

## Spec

Aegis ARCHITECTURE v0.6 + v0.7 增量.
