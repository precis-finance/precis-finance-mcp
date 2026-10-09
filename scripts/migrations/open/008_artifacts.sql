-- Target: PostgreSQL platform DB. Shared artifact schema, 2026-10-08.
-- Append after historical open/002..007; their ledger entries survive squashes.
-- Redis snapshots are deliberately not persisted.
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    basename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    source TEXT NOT NULL CHECK (source IN ('user_upload','agent_tool','sandbox')),
    producing_tool TEXT,
    version BIGINT NOT NULL DEFAULT 1,
    provenance JSONB NOT NULL DEFAULT '{}',
    access JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_files_user_active ON files(user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_files_expires ON files(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_uploads (
    upload_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    basename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    storage_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','receiving','uploaded','complete')),
    file_id TEXT REFERENCES files(file_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifact_uploads_user ON artifact_uploads(user_id,expires_at);

-- Retired immutable blob versions remain quota-accounted until unlink succeeds.
CREATE TABLE IF NOT EXISTS artifact_blob_gc (
    storage_path TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0)
);
