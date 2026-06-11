-- External identity join key for mode C (PRECIS_IDENTITY_COLUMN=external_id):
-- the stable identifier an external IdP emits (e.g. an immutable subject/oid),
-- mapped to the platform user. This is auth-grain — a verification join key, not
-- identity PII — so it lives on the open users table, not commercial
-- user_profile_ext. Nullable: mode-B users (who authenticate by
-- precis_user_id == users.id) leave it NULL.
--
-- IDEMPOTENT. Safe to re-run.

ALTER TABLE users ADD COLUMN IF NOT EXISTS external_id TEXT;

-- Unique only among non-null values (most users have no external_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_id
    ON users (external_id) WHERE external_id IS NOT NULL;
