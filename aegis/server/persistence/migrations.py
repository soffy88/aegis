"""SQL migrations runner — applies all migrations in order."""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS aegis_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# Inline migrations — easier than loading from files for v0.1
MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_event_trail",
        """
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE TABLE IF NOT EXISTS event_trail (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            org_id UUID NOT NULL,
            project_id UUID NOT NULL,
            user_id UUID,
            service TEXT,
            resource TEXT,
            environment TEXT DEFAULT 'prod',
            event_type TEXT NOT NULL,
            severity TEXT,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            trace_id TEXT,
            parent_id UUID,
            root_cause_id UUID,
            omodul_fingerprint TEXT,
            omodul_kind TEXT,
            autoheal_plugin TEXT,
            autoheal_result JSONB,
            initiated_by TEXT,
            approved_by UUID
        );

        CREATE INDEX IF NOT EXISTS idx_event_trail_tenant_time
            ON event_trail(org_id, project_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_event_trail_trace
            ON event_trail(trace_id) WHERE trace_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_event_trail_root_cause
            ON event_trail(root_cause_id) WHERE root_cause_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_event_trail_type_sev
            ON event_trail(event_type, severity, ts DESC);
        """,
    ),
    (
        "002_orgs_projects_users",
        """
        CREATE TABLE IF NOT EXISTS orgs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            created_at TIMESTAMPTZ DEFAULT now(),
            stripe_customer_id TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS projects (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            environment TEXT DEFAULT 'prod',
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(org_id, name)
        );

        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT UNIQUE NOT NULL,
            hashed_password TEXT,
            default_org_id UUID REFERENCES orgs(id),
            created_at TIMESTAMPTZ DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS org_memberships (
            org_id UUID REFERENCES orgs(id) ON DELETE CASCADE,
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
            PRIMARY KEY (org_id, user_id)
        );
        """,
    ),
    (
        "003_installed_apps",
        """
        CREATE TABLE IF NOT EXISTS installed_apps (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            app_name TEXT NOT NULL,
            app_version TEXT,
            install_dir TEXT NOT NULL,
            domain TEXT,
            status TEXT NOT NULL DEFAULT 'installing',
            installed_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(org_id, project_id, app_name)
        );

        CREATE TABLE IF NOT EXISTS domains (
            domain TEXT PRIMARY KEY,
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            target_url TEXT NOT NULL,
            tls_enabled BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        """,
    ),
    (
        "004_seed_self_hosted_defaults",
        """
        -- Self-hosted default org/project (used when no X-Org-Id / X-Project-Id header)
        -- IDs match aegis/server/api/deps.py: require_org / require_project defaults

        INSERT INTO orgs (id, name, plan)
        VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'enterprise')
        ON CONFLICT (id) DO NOTHING;

        INSERT INTO projects (id, org_id, name, environment)
        VALUES (
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000001',
            'default',
            'prod'
        )
        ON CONFLICT (id) DO NOTHING;
        """,
    ),
    (
        "005_event_trail_unique_fingerprint",
        """
        -- ADR-002 M1: omodul_fingerprint UNIQUE (方案 A, 同 fp 只保留首次)
        -- 去重: 保留最旧记录 (id 最小)
        DELETE FROM event_trail a
        USING event_trail b
        WHERE a.id > b.id
          AND a.omodul_fingerprint = b.omodul_fingerprint
          AND a.omodul_fingerprint IS NOT NULL;

        -- M2 预留: attempt_no 列
        ALTER TABLE event_trail
        ADD COLUMN IF NOT EXISTS attempt_no INTEGER DEFAULT 1;

        -- UNIQUE 约束
        ALTER TABLE event_trail
        DROP CONSTRAINT IF EXISTS event_trail_omodul_fingerprint_unique;

        ALTER TABLE event_trail
        ADD CONSTRAINT event_trail_omodul_fingerprint_unique
        UNIQUE (omodul_fingerprint);
        """,
    ),
    (
        "006_multitenancy_upgrade",
        """
        -- ===== orgs: add slug =====
        ALTER TABLE orgs ADD COLUMN IF NOT EXISTS slug VARCHAR(50);

        UPDATE orgs
        SET slug = lower(regexp_replace(name, '[^a-zA-Z0-9]+', '-', 'g'))
        WHERE slug IS NULL;

        UPDATE orgs
        SET slug = trim(both '-' from substring(slug from 1 for 50))
        WHERE slug IS NOT NULL;

        UPDATE orgs SET slug = 'org-' || substring(id::text from 1 for 8)
        WHERE slug = '' OR slug IS NULL;

        ALTER TABLE orgs ADD CONSTRAINT orgs_slug_unique UNIQUE (slug);
        ALTER TABLE orgs ADD CONSTRAINT orgs_slug_format CHECK (slug ~ '^[a-z0-9-]{1,50}$');
        ALTER TABLE orgs ALTER COLUMN slug SET NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_orgs_slug ON orgs(slug);

        ALTER TABLE orgs DROP CONSTRAINT IF EXISTS orgs_plan_check;
        ALTER TABLE orgs
        ADD CONSTRAINT orgs_plan_check
        CHECK (plan IN ('free', 'pro', 'enterprise'));

        -- ===== users: RENAME hashed_password + add 3 columns =====
        ALTER TABLE users RENAME COLUMN hashed_password TO password_hash;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(100);
        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_format;
        ALTER TABLE users ADD CONSTRAINT users_email_format CHECK (email ~ '^[^@]+@[^@]+\\.[^@]+$');

        -- ===== org_memberships: add operator role + joined_at =====
        ALTER TABLE org_memberships DROP CONSTRAINT IF EXISTS org_memberships_role_check;
        ALTER TABLE org_memberships ADD CONSTRAINT org_memberships_role_check
            CHECK (role IN ('owner', 'admin', 'operator', 'member', 'viewer'));
        ALTER TABLE org_memberships
        ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        -- ===== projects: add slug / display_name / docker_labels / config / archived_at =====
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS slug VARCHAR(50);
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS display_name VARCHAR(100);
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS docker_labels JSONB;
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS config JSONB;
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

        UPDATE projects
        SET slug = lower(regexp_replace(name, '[^a-zA-Z0-9]+', '-', 'g'))
        WHERE slug IS NULL;

        UPDATE projects
        SET slug = trim(both '-' from substring(slug from 1 for 50))
        WHERE slug IS NOT NULL;

        UPDATE projects SET slug = 'proj-' || substring(id::text from 1 for 8)
        WHERE slug = '' OR slug IS NULL;

        UPDATE projects SET display_name = name WHERE display_name IS NULL;

        ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_slug_format;
        ALTER TABLE projects ADD CONSTRAINT projects_slug_format CHECK (slug ~ '^[a-z0-9-]{1,50}$');
        ALTER TABLE projects ALTER COLUMN slug SET NOT NULL;
        ALTER TABLE projects ALTER COLUMN display_name SET NOT NULL;

        ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_org_slug_unique;
        ALTER TABLE projects ADD CONSTRAINT projects_org_slug_unique UNIQUE (org_id, slug);

        CREATE INDEX IF NOT EXISTS idx_projects_archived_at ON projects(archived_at)
        WHERE archived_at IS NOT NULL;
        """,
    ),
    # Migration 007: SKIPPED
    # event_trail / installed_apps / domains in BATCH 17 migration 002/003 already have project_id
    (
        "008_backfill_new_columns",
        """
        -- Ensure default org has slug='default'
        UPDATE orgs SET slug = 'default'
        WHERE id = '00000000-0000-0000-0000-000000000001' AND slug != 'default';

        -- Ensure default project has slug='default', display_name filled
        UPDATE projects SET slug = 'default', display_name = COALESCE(display_name, name)
        WHERE id = '00000000-0000-0000-0000-000000000002' AND slug != 'default';
        """,
    ),
    (
        "009_revoked_tokens",
        """
        CREATE TABLE IF NOT EXISTS revoked_tokens (
            jti         VARCHAR(36) PRIMARY KEY,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at  TIMESTAMPTZ NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires
            ON revoked_tokens(expires_at);
        """,
    ),
    (
        "010_alert_rules",
        """
        CREATE TABLE IF NOT EXISTS alert_rules (
            rule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id),
            project_id UUID NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            metric TEXT NOT NULL,
            threshold_warn REAL,
            threshold_critical REAL,
            operator TEXT NOT NULL DEFAULT '>='
                CHECK (operator IN ('>=', '>', '<', '<=', '==')),
            throttle_seconds INT NOT NULL DEFAULT 300
                CHECK (throttle_seconds >= 0),
            escalation_delay_seconds INT NOT NULL DEFAULT 1800
                CHECK (escalation_delay_seconds >= 0),
            dedup_bucket_seconds INT NOT NULL DEFAULT 3600
                CHECK (dedup_bucket_seconds > 0),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (org_id, project_id, name),
            CHECK (threshold_warn IS NOT NULL OR threshold_critical IS NOT NULL)
        );

        CREATE INDEX IF NOT EXISTS idx_alert_rules_active
            ON alert_rules(org_id, project_id) WHERE enabled = TRUE;
        """,
    ),
    (
        "011_alert_fired_history",
        """
        CREATE TABLE IF NOT EXISTS alert_fired_history (
            fired_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_id UUID NOT NULL REFERENCES alert_rules(rule_id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES orgs(id),
            project_id UUID NOT NULL REFERENCES projects(id),
            dedup_key TEXT NOT NULL UNIQUE,
            severity TEXT NOT NULL CHECK (severity IN ('warn', 'critical')),
            current_value REAL,
            triggered_reason TEXT,
            fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            escalated_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_alert_fired_rule_fired
            ON alert_fired_history(rule_id, fired_at DESC);
        CREATE INDEX IF NOT EXISTS idx_alert_fired_project_active
            ON alert_fired_history(org_id, project_id) WHERE escalated_at IS NULL;
        """,
    ),
    (
        "012_release_gates",
        """
        CREATE TABLE IF NOT EXISTS release_gates (
            gate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id),
            project_id UUID NOT NULL REFERENCES projects(id),
            autoheal_event_id UUID,
            action_kind TEXT NOT NULL,
            action_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            requested_by UUID NOT NULL REFERENCES users(id),
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'approved', 'rejected', 'expired')),
            decided_by UUID REFERENCES users(id),
            decided_at TIMESTAMPTZ,
            decision_reason TEXT,
            expires_at TIMESTAMPTZ NOT NULL,
            CHECK (
                (decided_by IS NULL AND decided_at IS NULL) OR
                (decided_by IS NOT NULL AND decided_at IS NOT NULL)
            ),
            UNIQUE (autoheal_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_release_gates_pending
            ON release_gates(org_id, project_id, state, expires_at)
            WHERE state = 'pending';
        CREATE INDEX IF NOT EXISTS idx_release_gates_history
            ON release_gates(org_id, project_id, requested_at DESC);
        """,
    ),
    (
        "013_webhooks",
        """
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            sub_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id),
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            secret_encrypted TEXT,
            event_types TEXT[] NOT NULL,
            retry_count INT NOT NULL DEFAULT 3 CHECK (retry_count >= 0 AND retry_count <= 10),
            retry_backoff_seconds INT[] NOT NULL DEFAULT '{5,15,45}',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(org_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_webhook_subs_active
            ON webhook_subscriptions(org_id) WHERE enabled = TRUE;

        CREATE TABLE IF NOT EXISTS webhook_delivery_queue (
            delivery_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sub_id UUID NOT NULL REFERENCES webhook_subscriptions(sub_id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES orgs(id),
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            attempt_no INT NOT NULL DEFAULT 0,
            max_attempts INT NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_attempt_at TIMESTAMPTZ,
            last_status_code INT,
            last_error TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'in_flight', 'succeeded', 'failed', 'dead_letter')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            succeeded_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_webhook_delivery_pending
            ON webhook_delivery_queue(state, next_attempt_at)
            WHERE state IN ('pending', 'in_flight');
        CREATE INDEX IF NOT EXISTS idx_webhook_delivery_history
            ON webhook_delivery_queue(sub_id, created_at DESC);
        """,
    ),
    (
        "014_timescaledb_extension",
        """
        CREATE EXTENSION IF NOT EXISTS timescaledb;
        """,
    ),
    (
        "015_error_events_and_issues",
        """
        -- error_events: hypertable, 1-day chunk, 7-day compression
        -- TimescaleDB requires partitioning col (ts) in PRIMARY KEY
        CREATE TABLE IF NOT EXISTS error_events (
            event_id UUID NOT NULL DEFAULT gen_random_uuid(),
            issue_id UUID,
            org_id UUID NOT NULL REFERENCES orgs(id),
            project_id UUID NOT NULL REFERENCES projects(id),
            fingerprint TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            exception_type TEXT NOT NULL,
            exception_value TEXT,
            level TEXT NOT NULL DEFAULT 'error'
                CHECK (level IN ('debug', 'info', 'warning', 'error', 'fatal')),
            environment TEXT DEFAULT 'prod',
            server_name TEXT,
            release_name TEXT,

            stacktrace JSONB,
            breadcrumbs JSONB,
            user_context JSONB,
            tags JSONB,
            extra JSONB,

            sdk_name TEXT,
            sdk_version TEXT,
            platform TEXT,

            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            PRIMARY KEY (event_id, ts)
        );

        SELECT create_hypertable('error_events', 'ts', chunk_time_interval => INTERVAL '1 day');

        ALTER TABLE error_events SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'org_id, project_id, fingerprint',
            timescaledb.compress_orderby = 'ts DESC'
        );
        SELECT add_compression_policy('error_events', INTERVAL '7 days');

        CREATE INDEX IF NOT EXISTS idx_error_events_issue
            ON error_events(issue_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_error_events_project_fp
            ON error_events(org_id, project_id, fingerprint, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_error_events_release
            ON error_events(release_name) WHERE release_name IS NOT NULL;

        -- error_issues: 聚合, UNIQUE(org_id, project_id, fingerprint)
        CREATE TABLE IF NOT EXISTS error_issues (
            issue_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id),
            project_id UUID NOT NULL REFERENCES projects(id),
            fingerprint TEXT NOT NULL,

            exception_type TEXT NOT NULL,
            exception_value TEXT,
            title TEXT GENERATED ALWAYS AS (
                COALESCE(exception_type, 'Error')
                || ': ' || COALESCE(LEFT(exception_value, 200), '')
            ) STORED,

            event_count BIGINT NOT NULL DEFAULT 1,
            user_count INT NOT NULL DEFAULT 0,

            first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            state TEXT NOT NULL DEFAULT 'unresolved'
                CHECK (state IN ('unresolved', 'resolved', 'ignored')),

            first_release TEXT,
            last_release TEXT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            UNIQUE(org_id, project_id, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_error_issues_project_last_seen
            ON error_issues(org_id, project_id, last_seen DESC);
        CREATE INDEX IF NOT EXISTS idx_error_issues_unresolved
            ON error_issues(org_id, project_id, last_seen DESC)
            WHERE state = 'unresolved';
        """,
    ),
    (
        "016_projects_sentry_public_key",
        """
        -- C3-6: DSN public key for Sentry SDK authentication.
        -- M1: auto-generated, query projects table for DSN.
        -- M3+: console UI display + rotate (AEGIS-BACKLOG-028).
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS sentry_public_key TEXT UNIQUE NOT NULL
            DEFAULT replace(gen_random_uuid()::text, '-', '');

        CREATE INDEX IF NOT EXISTS idx_projects_sentry_public_key
            ON projects(sentry_public_key);
        """,
    ),
    (
        "017_agent_metrics",
        """
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
            hostname    TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value       DOUBLE PRECISION NOT NULL,
            unit        TEXT NOT NULL DEFAULT '',
            tags        JSONB NOT NULL DEFAULT '{}'::jsonb
        );

        CREATE INDEX IF NOT EXISTS idx_agent_metrics_host_ts
            ON agent_metrics(hostname, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_metrics_name_ts
            ON agent_metrics(metric_name, ts DESC);
        """,
    ),
    (
        "018_invites",
        """
        CREATE TABLE IF NOT EXISTS org_invites (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id      UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            email       TEXT NOT NULL,
            role        TEXT NOT NULL,
            token       TEXT UNIQUE NOT NULL,
            invited_by  UUID NOT NULL REFERENCES users(id),
            expires_at  TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '7 days'),
            accepted_at TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_org_invites_token ON org_invites(token);
        CREATE INDEX IF NOT EXISTS idx_org_invites_email ON org_invites(email);
        CREATE INDEX IF NOT EXISTS idx_org_invites_org   ON org_invites(org_id);
        """,
    ),
    (
        "019_incidents",
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id          UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at     TIMESTAMPTZ,
            severity        TEXT NOT NULL DEFAULT 'warning',
            status          TEXT NOT NULL DEFAULT 'open',
            postmortem_md   TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_incidents_org_status
            ON incidents(org_id, status);
        CREATE INDEX IF NOT EXISTS idx_incidents_org_started
            ON incidents(org_id, started_at DESC);

        -- event_trail parent_id / root_cause_id already added in 001_event_trail.
        -- Add index on root_cause_id for fast incident event grouping.
        CREATE INDEX IF NOT EXISTS idx_event_trail_root_cause
            ON event_trail(root_cause_id) WHERE root_cause_id IS NOT NULL;
        """,
    ),
    (
        "020_nodes",
        """
        CREATE TABLE IF NOT EXISTS aegis_nodes (
            node_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            host TEXT NOT NULL,
            node_label TEXT NOT NULL,
            docker_mode TEXT NOT NULL,
            docker_host_url TEXT,
            server_version TEXT,
            os TEXT,
            arch TEXT,
            cpus INTEGER,
            memory_bytes BIGINT,
            registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(org_id, node_label)
        );
        """,
    ),
    (
        "021_autoheal_events",
        """
        CREATE TABLE IF NOT EXISTS aegis_alert_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cycle_id UUID NOT NULL,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            reason TEXT NOT NULL,
            value DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            handled BOOLEAN NOT NULL DEFAULT false,
            handled_at TIMESTAMPTZ,
            org_id UUID REFERENCES orgs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_autoheal_events_org_id
            ON aegis_alert_events(org_id);
        CREATE INDEX IF NOT EXISTS idx_autoheal_events_created_at
            ON aegis_alert_events(created_at DESC);
        """,
    ),
    (
        "022_backups",
        """
        CREATE TABLE IF NOT EXISTS aegis_backups (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            app_slug TEXT NOT NULL,
            instance_name TEXT NOT NULL,
            backup_key TEXT,
            size_bytes BIGINT DEFAULT 0,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            completed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_backups_org_app ON aegis_backups (org_id, app_slug);
        """,
    ),
    (
        "023_scrape_targets",
        """
        CREATE TABLE IF NOT EXISTS scrape_targets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            interval_seconds INT NOT NULL DEFAULT 30,
            labels JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            last_scrape_at TIMESTAMPTZ,
            last_status TEXT,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (org_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_scrape_targets_org ON scrape_targets (org_id);
        CREATE INDEX IF NOT EXISTS idx_scrape_targets_enabled
            ON scrape_targets (enabled) WHERE enabled = TRUE;
        """,
    ),
    (
        "024_incident_clustering",
        """
        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS dedup_key TEXT;
        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS event_count INT NOT NULL DEFAULT 0;
        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
        -- At most one OPEN incident per (org, dedup_key): new signals attach instead
        -- of spawning duplicates.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_open_dedup
            ON incidents (org_id, dedup_key)
            WHERE status = 'open' AND dedup_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS incident_events (
            incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            event_id    UUID NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (incident_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_incident_events_incident
            ON incident_events (incident_id);
        """,
    ),
    (
        "025_audit_log",
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            actor_user_id UUID,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_org_time
            ON audit_log (org_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action
            ON audit_log (org_id, action, created_at DESC);
        """,
    ),
    (
        "026_remediation_outcomes",
        """
        CREATE TABLE IF NOT EXISTS remediation_outcomes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            symptom_key TEXT NOT NULL,
            remediation TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            source TEXT NOT NULL DEFAULT 'runbook',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_remediation_outcomes_lookup
            ON remediation_outcomes (org_id, symptom_key, created_at DESC);
        """,
    ),
]


async def apply_migrations(conn: asyncpg.Connection) -> int:
    """Apply all pending migrations. Returns count applied.

    Serialized with a session-level Postgres advisory lock: concurrent boots
    (uvicorn --workers 2, multi-replica) would otherwise race on non-idempotent
    migrations (e.g. the 006 column rename) and crash one worker / half-apply the
    schema. The second runner blocks here until the first finishes, then sees all
    versions already applied and does nothing.
    """
    await conn.execute("SELECT pg_advisory_lock(hashtext('aegis_migrations'))")
    try:
        await conn.execute(_MIGRATIONS_TABLE)
        applied_rows = await conn.fetch("SELECT version FROM aegis_migrations")
        applied = {row["version"] for row in applied_rows}

        count = 0
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO aegis_migrations (version) VALUES ($1)",
                    version,
                )
            log.info("applied migration: %s", version)
            count += 1
        return count
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtext('aegis_migrations'))")
