-- Add 'admin_cli' to the assignment-source provenance CHECK so the open admin
-- CLI (precis_mcp/admin_cli.py) can record CLI-driven profile grants distinctly
-- from the admin UI ('admin_ui') and programmatic ('api') sources.
--
-- IDEMPOTENT. Drops the inline CHECK (auto-named) if present and re-adds it with
-- the extended value set. Safe to re-run.

ALTER TABLE user_profile_assignments
    DROP CONSTRAINT IF EXISTS user_profile_assignments_source_check;

ALTER TABLE user_profile_assignments
    ADD CONSTRAINT user_profile_assignments_source_check
    CHECK (source IN ('seed', 'admin_ui', 'api', 'sso_sync', 'admin_cli'));
