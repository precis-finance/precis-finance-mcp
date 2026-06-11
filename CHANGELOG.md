# Changelog

Notable changes to the `precis-mcp` open package. Format follows
[Keep a Changelog](https://keepachangelog.com/); the question every entry
answers is *"does this sync break my compose stack, my `instance/` files, or
my client integration?"*

<!-- Maintainers: add entries under [Unreleased] as part of every publish
     ritual (scripts/publish_open.py); move them under a dated heading when
     the sync is pushed to the mirror. -->

## [Unreleased]

Initial public release of the open core:

### Added

- Read-only MCP server: `run_statement`/`run_metric` (+`_data` variants),
  row-level inspection, discovery tools, ingestion status reads, and the
  `precis_orientation` tool; MCP Apps widgets (financial table, inspection
  grid) where the host supports them.
- Metric engine over a declarative model: YAML catalogue + SQL semantic
  layer in `instance/`, ClickHouse read layer with bundled/BYO data modes,
  federated read domains via Ibis (sum-only).
- Ingestion: declarative sources/bindings, extract → validate → atomic swap,
  cron scheduler and file-drop watcher daemons, `load_history` audit.
- Identity: three auth modes (dev key, bundled Keycloak — optionally
  brokered to a corporate IdP — or direct external OIDC), user profiles with
  scenario/domain/dimension scoping, per-call audit log.
- Backups: declarative `instance/backup.yml`, scheduled dump-tier bundles
  (Postgres + ClickHouse + instance config) to local or S3 destinations,
  checksum-verified restore and drills, `backup` compose profile.
- Deployment: single- and multi-user compose bundles, `deploy-mcp.sh`
  remote driver, optional OpenTelemetry instrumentation.
- Documentation site (`mkdocs`), `SECURITY.md`, DCO-based contribution flow
  via the one-way mirror.
