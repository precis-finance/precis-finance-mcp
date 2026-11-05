-- Forward upgrade for registries created before open/008. Do not amend 008:
-- existing deployment ledgers may already mark it applied.
-- Blob paths and file IDs are compatible; no filesystem changes are needed.
ALTER TABLE files
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS access JSONB NOT NULL DEFAULT '{}';

-- Old generated files (including tool-rewritten uploads) have no trustworthy
-- read-grant stamp. Preserve them, but fail closed rather than inventing grants
-- from today's permissions. grants:null cannot match permission_stamp().
-- Untouched uploads retain their existing owner-only access. Current artifacts
-- with a populated access stamp are never overwritten, including on re-apply.
UPDATE files
SET access = jsonb_build_object(
    'owner', user_id, 'grants', NULL, 'legacy_unverified', true
)
WHERE access = '{}'::jsonb
  AND (source <> 'user_upload' OR producing_tool IS NOT NULL);
