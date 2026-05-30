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
]


async def apply_migrations(conn: asyncpg.Connection) -> int:
    """Apply all pending migrations. Returns count applied."""
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
