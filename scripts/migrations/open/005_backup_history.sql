-- ---------------------------------------------------------------------------
-- backup_history — one row per backup run, restore, and restore drill
-- (docs/architecture/11-backup-and-dr.md invariant 6: drill outcomes are
-- observable). Written best-effort AFTER the bundle lands: a backup must
-- succeed even when Postgres is the store being backed up, so a failed
-- history write degrades to a log warning, never a failed run.
-- Composite PK: a drill of run X and the backup that produced run X share
-- run_id.
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
