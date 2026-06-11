-- precis_platform (open) — drop the tenant entity_id placeholder from users.
-- Target DB: precis_platform (PostgreSQL). Date: 2026-06-08.
--
-- entity_id was a multi-tenant placeholder never wired into the engine, the
-- scope/permission model, the catalogue, or any data-isolation path; it
-- defaulted to a fixture constant ('ENT-001') in every row. Removing it also
-- clears that fixture-constant leak from the open package.
-- See docs/archive_specs/entity_id_decommission_spec.md.
--
-- CASCADE: on a full (commercial) deployment the commercial user_full view
-- selects users.entity_id and depends on this column, so a plain DROP COLUMN
-- would fail. CASCADE drops that view; commercial/002 recreates it without
-- entity_id (commercial runs after open in the same migrate pass). On an
-- open-only deployment no such view exists and CASCADE drops nothing extra.
-- This is the only commercial object that depends on users.entity_id.

ALTER TABLE users DROP COLUMN IF EXISTS entity_id CASCADE;
