# Changelog

Notable changes to the `precis-mcp` open package. Format follows
[Keep a Changelog](https://keepachangelog.com/); the question every entry
answers is *"does this sync break my compose stack, my `instance/` files, or
my client integration?"*

<!-- Maintainers: add entries under [Unreleased] as part of every publish
     ritual (scripts/publish_open.py); move them under a dated heading when
     the sync is pushed to the mirror. -->

## [Unreleased]

## [0.2.3] - 2026-07-03

### Added

- **Provided ragged hierarchies, multi-parent rollups, and
  breakdown-by-hierarchy.** Ragged hierarchies are no longer limited to the
  *generated* case (ancestor columns denormalised on the leaf table). A
  hierarchy can now be **provided**: point `source.type: provided` in
  `dimensions.yml` at an operator-supplied node master (`node_table`) and
  child→parent edge table (`edge_table` with `child_column` /
  `parent_column`), and the platform derives everything else. Because
  topology now lives in an explicit edge object, a hierarchy supports ragged
  depth and a child rolling up into **more than one parent** (set membership
  — a leaf under two parents contributes its full value to each; weighted
  allocation is deferred). Generated and provided hierarchies share one
  representation: per hierarchy the platform derives three fixed-name
  semantic views — the flattened node list (`dim_<leaf>_<key>`), the edges
  (`…_edges`), and a recursive rollup (`…_rollup`) that replaces the old
  single-parent UNION-ALL derivation. Reporting tools can also now **break
  down by a hierarchy**: pass the hierarchy key itself in `dimensions` on
  `run_statement` / `run_metric` (typically with a node filter) and the
  figures split into that node's children, whatever level they sit at.

    **Upgrade action:** the new `…_edges` views must be applied to ClickHouse —
    run `python -m precis_mcp.clickhouse_init` after upgrading. A container
    restart alone does not re-apply semantic views, and hierarchy queries fail
    with `UNKNOWN_TABLE` until it runs.

- **Excel add-in: member drop-downs and dimension discovery.** The task pane
  now inserts native Excel data-validation drop-downs for any dimension —
  leaf, derived, or ragged (hierarchies list every node, all levels). Lists
  live on a hidden `PrecisLists` sheet as live `=PRECIS.HIERARCHY(…;"list")`
  spills behind workbook-level `Precis_List_<dim>_*` named ranges, so all
  drop-downs for a dimension share one list and the pane's **Refresh**
  updates them all. Two display modes (codes, or `code | name` with the code
  first for delimiter-safe extraction), plus a companion formula in the
  adjacent column. Supporting server changes: a new `list_dimensions` tool
  (catalogue metadata only — members remain `search_hierarchy`'s job),
  `search_hierarchy` now lists derived-dimension members and takes a `limit`
  (defaults unchanged; ceiling 10000), and a new `output="list"` mode on
  `PRECIS.HIERARCHY`. The pane also links to the hosted function docs.
  Manifest version is 1.0.0.4 — re-sideload or clear the Office cache to
  pick up the new pane.

### Changed

- `run_statement` / `run_metric` (the widget-linked variants) now hand the
  model the same raw engine result as the `_data` variants, with the rendered
  table carried on result `_meta` — hosts cache widget bundles per connector,
  so remove and re-add the connector if the widget looks stale.
- Unified results now carry dimension item codes alongside display labels.
- Rendered financial tables: two-tier headers on multi-scenario crosstabs,
  and total-column shading, freeze, and bounded column widths.

### Fixed

- Sync database and Keycloak calls no longer run on the event loop, so one
  slow query can't stall every in-flight stream; the permission gate's
  scenario lookups are now TTL-cached (new `PRECIS_SCENARIO_REGISTRY_TTL`,
  default 5s).
- `/excel` responses send `Cache-Control: no-cache`, so browsers revalidate
  the add-in bundle and an upgrade never serves a stale task pane.

## [0.2.2] - 2026-06-25

A fix release on top of 0.2.1. The headline fix: enabling the Excel add-in no
longer breaks other MCP clients' authentication. No breaking changes — the
`/mcp` tools and their responses are unchanged.

### Fixed

- **Enabling the Excel add-in no longer breaks other MCP clients.** With
  `KC_ENABLE_EXCEL_ADDIN=true`, the add-in's requested scope set was advertised in
  the standard RFC 9728 `scopes_supported` field of the shared
  `/.well-known/oauth-protected-resource` document — which every MCP client reads.
  A generic client then requested that scope at dynamic client registration, and
  Keycloak derived the new client's scope set from the request, dropping the
  realm-default audience scope; the client's tokens lost the `/mcp` audience and
  every call returned 401. The hint now lives under a namespaced
  `precis_excel_scopes` key, so the shared document is identical whether or not
  the add-in is enabled. If you integrate the add-in against an external IdP, it
  reads `precis_excel_scopes` (previously `scopes_supported`); the
  `EXCEL_ADDIN_SCOPE` / `EXCEL_ADDIN_RESOURCE_INDICATOR` knobs are unchanged.
- **The Keycloak realm reconcile runs on every mode-B deploy.** `deploy-mcp.sh`
  now re-applies the realm reconcile on each deploy, not only when Compose
  recreates the one-shot — so a `PRECIS_BASE_URL` / domain change always restamps
  the `/mcp` audience mapper, redirect URIs, and web origins. The server also logs
  a warning when an incoming token's audience does not match the expected `/mcp`
  value, surfacing audience drift before it turns into a silent 401.

## [0.2.1] - 2026-06-25

A fix release on top of 0.2.0: the published image now serves the MCP widgets,
the single-user quickstart pulls the current release, and first-admin creation
is built into the deploy. No breaking changes — the `/mcp` tools and their
responses are unchanged.

### Fixed

- **MCP widgets now ship in the published image.** `run_statement` / `run_metric`
  carry a financial-table widget and `inspect_rows` an inspection-grid widget for
  hosts that render MCP UI — but the prebuilt bundles were silently dropped from
  the image (an over-broad `dist/` ignore also matched `ui/mcp-widgets/dist/`), so
  the server fell back to text. The bundles are now packaged; widget-capable hosts
  render the table, and other hosts keep getting the full figures as before.
- **The single-user quickstart pulls the current release.** `docker-compose.local.yml`
  pinned `PRECIS_MCP_TAG` to an older default, so a fresh quickstart pulled a
  stale image; it now tracks the release.

### Added

- **First-admin bootstrap in `deploy-mcp.sh`.** `--admin-id <id>` (or
  `PRECIS_BOOTSTRAP_ADMIN_ID` in `deploy/.env`) creates the first admin during
  deploy — idempotently — and prints a one-time temporary password in the deploy
  output; run without it, the deploy prints the exact manual command. In
  bundled-Keycloak mode the Keycloak bootstrap-admin credential is injected only
  for that one call, so the long-running server never carries it.

## [0.2.0] - 2026-06-25

Adds the **read-only Excel add-in** as an open feature, served by the published
image at `/excel`. No client-integration break — the `/mcp` read tools and their
response shapes are unchanged, and the add-in stays off until you enable it. One
compose change matters if you bring your own `instance/`: the single-user local
bundle now defaults to the image's baked-in demo instance; mount your own model
through the new `docker-compose.instance.yml` overlay (see **Changed**).

### Added

- **Excel add-in (read-only).** A Microsoft Excel add-in that brings live
  statements and metrics into the grid as `PRECIS.*` custom functions
  (`STATEMENT`, `METRIC`, `HIERARCHY`, `KPIS`, `SCENARIOS`), with **Format** and
  **Refresh** ribbon actions. It is an OAuth/MCP client of your own instance's
  `/mcp` endpoint — no separate account, and figures never leave your instance.
  The published image ships the built bundle and serves it at `/excel`; those
  static assets carry app-owned security headers (open CORS, framable CSP) so
  Office can host the task pane. Works in the bundled-Keycloak (including
  brokered) and direct-external-OIDC identity modes; dev-key mode is not
  supported. See [Précis for Excel](docs/excel/index.md).
- **Add-in OAuth client provisioning.** `EXCEL_ADDIN_ENABLED` gates the `/excel`
  surface. Bundled Keycloak: `KC_ENABLE_EXCEL_ADDIN` + `KC_ADDIN_REDIRECT_URIS`
  reconcile a gated public `precis-excel-addin` client (deleted again when the
  flag is off). External OIDC: `EXCEL_ADDIN_CLIENT_ID`, plus `EXCEL_ADDIN_SCOPE`
  and `EXCEL_ADDIN_RESOURCE_INDICATOR` for IdPs that bind the audience via a
  scope rather than the `resource` parameter. `EXCEL_ADDIN_DIST_DIR` overrides
  the served bundle directory.
- **Read-path concurrency caps.** Two semaphores bound the read path so a
  workbook refresh (one tool call per cell) cannot swamp ClickHouse:
  `PRECIS_MAX_CONCURRENT_READS_PER_USER` (per principal) and
  `PRECIS_MAX_CONCURRENT_READS_GLOBAL` (process-wide).
- **Package-only single-user quickstart.** `docker-compose.local.yml` pulls the
  published image and serves its baked-in demo instance with no instance mount,
  so first run needs no source checkout — download one compose file and bring
  the stack up.

### Changed

- **`deploy-mcp.sh` pulls the published image by default.** Building from source
  is now opt-in (`--build`, `--tag`, or `--extras`, which implies `--build`); the
  driver also waits for ClickHouse readiness before generating the sample
  bundle. Existing build workflows are unaffected via `--build`.
- **Bring-your-own `instance/` on the local bundle moves to an overlay.** The
  single-user `docker-compose.local.yml` now defaults to the image's demo
  instance with no mount. To run your own model, add the new
  `docker-compose.instance.yml` overlay, which bind-mounts your `instance/`.

## [0.1.1] - 2026-06-20

First release published as a container image. Beyond the published image and
the compose pull/build model, it carries an engine change to the catalogue
dimension contract and adds hierarchy-breakdown resolution — review the
**Changed** section below if you maintain `instance/` catalogues.

### Added

- Published container image `ghcr.io/precis-finance/precis-mcp`, built and
  pushed on each `v*` release. The compose bundles reference it by default and
  fall back to building from source when the tag is absent (`PRECIS_MCP_TAG`).
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
- Hierarchy breakdowns without denormalisation: a derived/parent dimension
  (e.g. department, division, grade) can be used as a breakdown directly — the
  engine joins the leaf dimension at query time and groups by its value column,
  so adding a node to a hierarchy no longer means editing every fact view.
  Period parents (quarter/fiscal_year) stay fact-view columns; federated read
  domains require the axis to be a physical column on the foreign view, else the
  query errors clearly.

### Changed

- The app services now reference a published `image:` with a `build:` fallback.
  `docker compose up` pulls the release image by default; pass `--build` (or
  set `PRECIS_MCP_TAG` to a tag you build yourself) to build from source as
  before. Existing `up --build` workflows are unaffected.
- **Catalogue dimension contract — review your `instance/` catalogues.** A cube
  dimension's `key` is now the catalogue dimension name (the single key for both
  filters and breakdowns) and `source` is the physical view column (defaults to
  `key`). Result rows are keyed by the catalogue name, so a view can rename a
  dimension column without changing the agent vocabulary. Definitions that
  relied on the old behaviour — `key` as the master-dimension key, the raw
  column emitted in `GROUP BY`, view columns aliased to match internal keys —
  must be updated.
- `list_kpis` returns a single `dimension_keys` field, replacing the separate
  `available_dimensions` and `filter_keys` (client integration change).

### Fixed

- The deploy env template (`deploy/.env.example`), referenced throughout the
  docs, is now included in the published repository — a `.gitignore` glob had
  dropped it from every export.
