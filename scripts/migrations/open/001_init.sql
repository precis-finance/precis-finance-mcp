-- precis_platform — OPEN core schema (squashed init).
--
-- The open half of the platform database: identity-as-credential, the
-- profile/permission model, the security + inspection audit logs, the
-- plan-validation rule store, and the ingestion load-history provenance record.
-- It is the schema the open `precis-mcp` package ships and the FIRST root the
-- migration runner applies (open before commercial — every foreign key points
-- commercial -> open, never the reverse).
--
-- ORIGIN. Squash of the open-owned schema at its FINAL shape: the original flat
-- history (platform/001..030) with the post-split open migrations (open/002..007)
-- folded in. Captured 2026-06-17, when every live deployment was at HEAD or being
-- rebuilt fresh — the precondition that makes a squash safe. Per-step history is
-- in git; do not reconstruct it.
--
-- IDEMPOTENT. Every statement guards with IF NOT EXISTS, so applying this to a
-- database that already has the schema (any current deployment) is a no-op; a
-- fresh database gets the full open schema. No demo/fixture seed lives here —
-- seeding profiles/users is a deployment concern, not schema.
--
-- INVARIANT. No statement in this file may reference a commercial table. An
-- open -> commercial foreign key is a boundary violation.

-- ---------------------------------------------------------------------------
-- users — identity-as-credential grain. The five personalization columns
-- (identity, preferences, skill_preferences, report_context, onboarded_at) live
-- in the commercial user_profile_ext table, not here. external_id is the mode-C
-- external-IdP join key (the stable subject/oid an IdP emits); NULL for mode-B
-- users who authenticate by precis_user_id == users.id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    is_admin    BOOLEAN NOT NULL DEFAULT false,
    is_disabled BOOLEAN NOT NULL DEFAULT false,
    external_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique only among non-null values (most users have no external_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_id
    ON users (external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- profiles — reusable security definitions (mutable; history in profile_audit).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    profile_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    definition  JSONB NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- profile_audit — append-only history of every profile create/update/delete.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profile_audit (
    audit_id      BIGSERIAL PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    definition    JSONB NOT NULL,
    changed_by    TEXT NOT NULL,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_reason TEXT,
    change_kind   TEXT NOT NULL CHECK (change_kind IN ('create', 'update', 'delete'))
);

CREATE INDEX IF NOT EXISTS idx_profile_audit_profile_id
    ON profile_audit (profile_id, changed_at DESC);

-- ---------------------------------------------------------------------------
-- user_profile_assignments — bind each user to at most one profile.
-- FK to profiles has NO CASCADE: revoke the assignment before deleting a
-- profile, so access grants are never silently lost. The source provenance
-- distinguishes seed / admin UI / API / SSO sync / open admin CLI grants.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profile_assignments (
    user_id    TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT '',
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,                              -- NULL = permanent
    source     TEXT NOT NULL DEFAULT 'admin_ui'
               CHECK (source IN ('seed', 'admin_ui', 'api', 'sso_sync', 'admin_cli')),
    FOREIGN KEY (profile_id) REFERENCES profiles(profile_id)
);

CREATE INDEX IF NOT EXISTS idx_upa_profile_id
    ON user_profile_assignments (profile_id);

-- ---------------------------------------------------------------------------
-- security_audit_log — append-only record of security-relevant events. trace_id
-- joins a row to the OpenTelemetry trace of the turn that produced it; populated
-- on the MCP / agent tool-call path, NULL for rows written off-span (admin CLI,
-- headless operator ops).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_audit_log (
    id             BIGSERIAL PRIMARY KEY,
    event_type     TEXT NOT NULL,
    actor_id       TEXT NOT NULL,
    target_user_id TEXT,
    scenario_id    TEXT,
    details        JSONB NOT NULL DEFAULT '{}',
    trace_id       TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_security_audit_log_actor
    ON security_audit_log (actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_audit_log_target
    ON security_audit_log (target_user_id, created_at DESC)
    WHERE target_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_security_audit_log_scenario
    ON security_audit_log (scenario_id, created_at DESC)
    WHERE scenario_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_security_audit_log_trace
    ON security_audit_log (trace_id)
    WHERE trace_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- validation_rules — append-only, versioned plan-validation rule store, keyed
-- by (scenario_id, dataset_key, updated_at). Loaders take the latest per key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_rules (
    scenario_id TEXT        NOT NULL,
    dataset_key TEXT        NOT NULL DEFAULT 'gl_plan',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT        NOT NULL DEFAULT '',
    rules_code  TEXT        NOT NULL DEFAULT '',
    created_by  TEXT        NOT NULL DEFAULT '',
    PRIMARY KEY (scenario_id, dataset_key, updated_at)
);

CREATE INDEX IF NOT EXISTS idx_validation_rules_scenario_latest
    ON validation_rules (scenario_id, dataset_key, updated_at DESC);

-- ---------------------------------------------------------------------------
-- load_history — durable provenance per ingestion attempt. REPLACE PARTITION
-- discards the prior partition, so this is the only lasting record of what
-- loaded for a (source, dataset, period, scenario), when, by whom, with what
-- control totals. Append-only; rows are updated only to fill terminal status.
-- The period CHECK is loose on purpose: finance periods are not always calendar
-- months ('YYYY-MM', 'YYYY-13', 'YYYY-MM-ADJ' all valid). 'failed_checks' is the
-- terminal bucket for a load that failed an operator-declared data-quality check
-- (distinct from 'failed_recon', a structural shape drift); warning/info check
-- outcomes do not change status (their detail lives in control_total_result).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS load_history (
    load_id              TEXT PRIMARY KEY,
    binding_id           TEXT NOT NULL,
    source_id            TEXT NOT NULL,
    dataset_id           TEXT NOT NULL,
    period               TEXT NOT NULL,
    scenario_id          TEXT NOT NULL,
    status               TEXT NOT NULL,
    triggered_by         TEXT NOT NULL,
    rows_landed          BIGINT,
    source_manifest      JSONB NOT NULL DEFAULT '{}'::jsonb,
    control_total_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    duration_ms          INTEGER,
    swap_committed_at    TIMESTAMPTZ,
    dbt_refreshed_at     TIMESTAMPTZ,
    error_message        TEXT,
    notes                TEXT,
    CONSTRAINT load_history_status_check CHECK (
        status IN (
            'running',
            'success',
            'failed_extract',
            'failed_recon',
            'failed_swap',
            'failed_dbt',
            'failed_validation',
            'failed_other',
            'failed_checks'
        )
    ),
    CONSTRAINT load_history_period_format_check CHECK (
        period ~ '^[0-9]{4}-.+$'
    )
);

CREATE INDEX IF NOT EXISTS idx_load_history_binding_period_started
    ON load_history (binding_id, period, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_load_history_status_started
    ON load_history (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_load_history_dataset_started
    ON load_history (dataset_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- inspection_audit — one row per row-level inspection query (read-tool inspect).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inspection_audit (
    audit_id     BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      TEXT NOT NULL DEFAULT '',
    source_key   TEXT NOT NULL,
    backend_kind TEXT NOT NULL DEFAULT '',
    backend      TEXT NOT NULL DEFAULT '',
    source_view  TEXT NOT NULL DEFAULT '',
    filters      JSONB NOT NULL DEFAULT '{}'::jsonb,
    columns      JSONB NOT NULL DEFAULT '[]'::jsonb,
    rendered_sql TEXT NOT NULL DEFAULT '',
    query_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    row_count    INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    truncated    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_inspection_audit_created_at
    ON inspection_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inspection_audit_user_created
    ON inspection_audit (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inspection_audit_source_created
    ON inspection_audit (source_key, created_at DESC);

-- ---------------------------------------------------------------------------
-- backup_history — one row per backup run, restore, and restore drill
-- (operations/backups.md: drill outcomes are observable). Written best-effort
-- AFTER the bundle lands: a backup must succeed even when Postgres is the store
-- being backed up, so a failed history write degrades to a log warning, never a
-- failed run. Composite PK: a drill of run X and the backup that produced run X
-- share run_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_history (
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    triggered_by  TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    duration_ms   INTEGER,
    outcome       TEXT NOT NULL,
    artifacts     JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest_key  TEXT,
    total_bytes   BIGINT,
    error_message TEXT,
    PRIMARY KEY (run_id, kind, started_at),
    CONSTRAINT backup_history_kind_check CHECK (
        kind IN ('backup', 'restore', 'restore_drill')
    ),
    CONSTRAINT backup_history_outcome_check CHECK (
        outcome IN ('success', 'failed', 'partial')
    ),
    CONSTRAINT backup_history_trigger_check CHECK (
        triggered_by IN ('cli', 'scheduler')
    )
);

CREATE INDEX IF NOT EXISTS idx_backup_history_started
    ON backup_history (started_at DESC);
