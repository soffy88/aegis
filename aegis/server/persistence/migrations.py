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
