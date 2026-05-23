# Project Health Protocol

Aegis monitors the health of registered projects by polling a standard HTTP endpoint.

## Endpoint

Each project SHOULD expose:

```
GET /health
```

Response: `application/json`

```json
{
  "status": "ok",
  "version": "1.2.3",
  "checks": {
    "db": {"status": "ok", "latency_ms": 12},
    "redis": {"status": "ok"}
  },
  "timestamp": "2026-05-23T00:00:00Z"
}
```

## Status semantics

| Value | Meaning |
|-------|---------|
| `ok` | All subsystems healthy, serving traffic normally |
| `degraded` | Partially functional — some checks failing but core service available |
| `down` | Not serving traffic, needs intervention |

## Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | `"ok" \| "degraded" \| "down"` | Yes | Overall health |
| `version` | `string \| null` | No | Deployed version |
| `checks` | `object` | No | Sub-component health (keys = component name) |
| `timestamp` | `ISO 8601 datetime` | Yes | When this health snapshot was generated |

### checks sub-object

Each key in `checks` is a component name. Value is an object with at minimum a `status` field (`"ok"`, `"degraded"`, or `"down"`). Additional fields (e.g., `latency_ms`, `error`) are optional.

## Backward compatibility

Projects that do NOT implement this schema are still supported:

- **HTTP 200** with any body → inferred as `status: "ok"`
- **HTTP 5xx** → inferred as `status: "down"`
- **Connection refused / timeout** → inferred as `status: "down"`

This means existing projects (helixa, tide, hevi, selene) work without changes.

## Polling behavior

- Aegis polls each project's health endpoint every **30 seconds**
- Results are cached with 30s TTL
- Timeout per request: 5 seconds
- On timeout: status = `down`

## Configuration

Projects declare their health endpoint via Docker labels (see below) or manual registration.
