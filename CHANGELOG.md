# Changelog

New features, fixes and upgrade requirements for users of the
`precis-finance-mcp` open package. Format follows
[Keep a Changelog](https://keepachangelog.com/).

<!-- Maintainers: add entries under [Unreleased]; move them under a dated
     version heading when releasing. -->

## [Unreleased]

## [0.4.0] - 2026-11-05

This release adds **Databricks as a primary analytical backend** and **live
Excel formulas from exported reports**. Use the same governed metrics,
financial statements and scenario comparisons on ClickHouse or Databricks,
then take an exported report into Excel and refresh it through the add-in.

### Added

- **Native Databricks reporting.** Select Databricks SQL as the deployment's
  primary backend for metric and statement queries, scenario comparisons,
  dimension and hierarchy discovery, scoped reads and bounded row inspection.
  The shared catalogue, financial calculations, permissions and result formats
  apply on both backends. ClickHouse remains the default.
- **Customer-managed analytical inputs.** Databricks deployments query the
  semantic views maintained by your data team, without requiring a ClickHouse
  copy. Your team loads and refreshes actuals and master data through its own
  pipelines. Précis does not run ingestion jobs or expose ingestion controls
  in this configuration.
- **Excel: convert a selected export to a live formula.** Select a region in
  a statement or metric XLSX downloaded through `export_dataset`, then choose
  **Convert selected export** in the add-in. Both report and extract layouts
  are supported. The selected region becomes a `PRECIS.STATEMENT` or
  `PRECIS.METRIC` spill at the same position. Headers, figures and a context
  footer refresh together.

### Changed

- **Excel add-in package identity.** Package metadata and conduct reporting now
  identify Précis. The built bundle carries the Précis license and the original
  Microsoft template's MIT notice.
- **Security dependency refresh.** Update the Python TLS trust bundle and MCP
  SDK, optional Snowflake driver, bundled PostgreSQL/Valkey/Keycloak images,
  Caddy toolchain and frontend dependencies. CI now gates frontend release-builder
  dependencies, including build tools, on High/Critical issues; the Excel builder
  excludes its unused developer login/sideload CLI peers. Full local developer
  installs retain Microsoft CLI MSAL/ZIP advisories, reported separately in CI.
- **Shared reporting execution.** Native reads reuse database connections and
  Databricks authentication. Independent financial queries run with bounded
  parallelism, and compatible additive/period-average metrics share a query.
  Reports retain deterministic result assembly and fail if a required query
  fails, rather than returning an incomplete statement.
- **Scenario metadata caching.** `PRECIS_SCENARIO_REGISTRY_TTL` now defaults to
  60 seconds instead of 5. Reporting reuses one scenario snapshot across its
  columns. Permissions and financial results are not cached by this mechanism;
  external metadata changes become visible after cache expiry and refresh.
- **Materialized hierarchy mappings.** Both backends can provision stored
  node-to-leaf mappings behind the existing rollup views. Reporting and scope
  resolution reuse those mappings instead of expanding the hierarchy each time.
- **Consistent empty sums.** On both native backends, SUM over no matching rows
  returns zero. A nonempty group containing only null values still returns null.
  This changes the former ClickHouse nullable-empty result from null to zero.
- **Shared Excel layouts and precision.** Static financial exports and live
  formulas use the same headers, semantic rows and single context footer.
  Extract Tables and value-column names exclude the footer. Values retain
  available engine/Excel precision; decimal settings control display.

### Upgrade required

- **Refresh bundled services.** Pull updated Valkey and Keycloak images and
  build PostgreSQL's embedded APT patch layer and bundled Caddy. Compose 2.17+
  is required; restarting the application alone does not update these services.
- **Apply pending open PostgreSQL migrations before starting the server.**
  Migration `009_upgrade_legacy_artifacts.sql` adds version, provenance and
  access metadata to legacy file registries while preserving file IDs and blob
  paths. Legacy generated files without recorded read grants must be
  regenerated under current permissions. Untouched uploads retain owner-only
  access. See [Upgrading](docs/operations/upgrades.md).
- **Databricks requires explicit setup.** Install the `databricks-primary`
  extra, or build the image with `PRECIS_EXTRAS=databricks-primary`. Configure
  `PRECIS_ANALYTICAL_BACKEND=databricks`, customer-managed ingestion, warehouse
  access through service-principal OAuth M2M, catalogue/schema mappings and
  compatible semantic inputs. Provision
  the scenario registry and hierarchy objects with operator credentials; keep
  the serving identity read-only. PostgreSQL and Valkey remain required. The
  supplied Compose quickstart remains ClickHouse-based; installing the extra
  alone does not configure a Databricks deployment. See
  [Analytical backend configuration](docs/configuration/analytical-client.md).
- **Provision and refresh hierarchies.** To adopt materialized mappings, run
  `precis-finance-mcp-admin hierarchy refresh --provision` with operator
  credentials. Précis-managed ClickHouse snapshot ingestion refreshes mappings
  after source updates; customer-managed pipelines must call
  `precis-finance-mcp-admin hierarchy refresh` after hierarchy changes. Serialize
  publication and keep affected reporting unavailable until all mappings are
  refreshed: replacement is atomic per mapping table, not across the full
  update. See [Hierarchy provisioning and refresh](docs/configuration/analytical-client.md#explicit-hierarchy-provisioning-and-refresh).
- **Update the Excel bundle and manifest together.** Add-in manifest version
  is `1.0.0.9`; reload or redeploy the updated hosted manifest through your
  installation route. Older exports and live markers without the new region
  and context-footer metadata must be regenerated. Both financial functions
  now accept at most nine arguments: remove the former tenth decimal-mode
  argument and use `decimals` for display precision. Selected conversion
  requires ExcelApi 1.13 or later. Fixed-cell references may point to different
  lines after a refresh changes row order. See [Précis for Excel](docs/excel/index.md)
  and the [function reference](docs/excel/functions.md).
- **Reconcile Keycloak user provisioning where used.** User lifecycle commands
  now use the scoped `precis-user-provisioner` service account, configured with
  `KC_PROVISIONING_CLIENT_SECRET` or its `_FILE` variant. Bootstrap administrator
  credentials remain operator-only and are no longer accepted by the user
  provisioning client. See [Sign-in and identity modes](docs/deployment/oauth-keycloak.md).

### Known security issue

- **Bundled Keycloak:** the upstream 26.8.1 image includes Guava affected by
  **CVE-2026-102554**, a denial-of-service issue when deserializing untrusted
  Java objects. The maintainers accepted this availability risk for the reviewed
  upstream image until **December 5, 2026, 00:00 UTC**. CI suppresses only this
  CVE in that image's Guava JAR; the exception expires independently of the next
  release. Exploit reachability in this deployment has not been disproved.
  Deployments using external OIDC do not run the bundled image.

### Fixed

- `make lock-extras` works from the public repository using its published
  metadata and core lock, without requiring private dependency inputs.

- Published tests now use dedicated public fixtures and helpers. Publication
  checks every shipped test-support file and rejects undeclared dependencies.
  Tests requiring optional Databricks or telemetry packages skip when those
  extras are absent, so the core development installation can run the suite.

- Initialize optional tracing in both MCP server entrypoints. Installing the
  telemetry extra leaves export disabled until explicitly enabled; the Compose
  bundles now forward the switch and collector settings. Signed artifact
  transfer routes remain excluded from tracing.

- Excel exports preserve formula-like source text as literal cells, and static
  and live outputs use consistent per-cell financial number formats.
- Backup restore rejects explicitly requested stores that have no successful
  artifact, rather than silently restoring only the available subset.
- ClickHouse ingestion, provisioning and sample-data connections honour the
  configured TLS verification and CA settings; the remote Compose bundle
  forwards the supported warehouse TLS configuration.

### Security

- Patched Debian APT in application and PostgreSQL images, refreshed the
  upstream Keycloak digest, and rebuilt Caddy 2.11.9 with patched Go dependencies.
  Optional warehouse drivers use pyasn1 0.6.5. Frontend builders use patched npm;
  CI audits both release-builder dependency sets and reports the separate Office
  developer-tool advisories.
- Allowed dimension-scope clauses resolving to the same leaf axis now
  intersect, preserving empty allowances as deny-all instead of broadening
  access. Exclusions continue to combine by union.
- **Resolved the Databricks authentication dependency finding
  ([CVE-2026-49265](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-xpv3-w29h-x7cv)).**
  Optional Databricks dependencies now use SQL Connector 4.7.0 with OAuthlib
  4.0.0, which includes the fix for the PKCE verification timing vulnerability.
  The earlier temporary audit exception has been removed. Existing
  optional-driver images need rebuilding to receive the fix.

Précis Finance MCP and its Excel add-in remain read-only for financial data.
Commercial planning and agent-side workbook conversion workflows belong to
the full Précis platform and are not added to the open MCP tool surface.

## [0.3.0] - 2026-10-09

This release adds **downloadable JSON/Excel extracts and charts** to the
standalone MCP server. Previously, the open package provided financial queries
and table widgets without these export or chart-creation capabilities.

### Added

- **File extracts over MCP.** Run a financial query, then pass its `data_ref`
  to `export_dataset` with `format="json"` or `format="xlsx"`. The tool exports
  that query snapshot without rerunning it and returns a download link.
  `get_file_download` renews an expired link while the file remains available.
- **Charts over MCP.** Pass query references as a `data_refs` array to
  `eval_chart_transform`, with a JavaScript transformation and chart specification.
  Nine chart types are supported, including line, bar, waterfall and scatter.
  Compatible clients display the new chart widget; other clients receive
  structured chart data.
- **Temporary result and file storage.** Query snapshots retain full numerical
  precision; generated files have ownership checks, size/storage limits and
  automatic expiry/cleanup. Signed downloads work independently of the agent
  host's filesystem; delivery into a host's Python sandbox remains host-dependent.

### Upgrade required

The new features require **Valkey, PostgreSQL migration `open/008`, a persistent
file volume and an artifact signing key**. Updating only the application image
or restarting the old stack is insufficient.

Update to this release's deployment files, retaining your existing configuration
and `instance/`. In `deploy/.env`, set `PRECIS_MCP_TAG=0.3.0`, keep your public
HTTPS `PRECIS_BASE_URL`, and add a distinct `ARTIFACT_SIGNING_KEY` generated once
with `openssl rand -hex 32`. Back up before upgrading. From the open repository:

```sh
mcp_compose() { docker compose -f deploy/docker-compose.yml --env-file deploy/.env "$@"; }
mcp_compose stop precis-mcp
mcp_compose pull precis-mcp migrate postgres redis
mcp_compose up -d --wait postgres redis
mcp_compose run --rm --no-deps migrate
mcp_compose up -d
```

These commands use the published 0.3.0 image, install the Valkey sidecar and
apply pending open migrations before starting the server. For **source builds**,
replace the `pull` command with
`mcp_compose pull postgres redis` followed by
`mcp_compose build precis-mcp migrate`; the image build installs the new Python
dependencies and builds the widgets. Standard 0.2.x open installations need no
file-table reset.

With bundled Caddy, run `mcp_compose build caddy` before the final `up -d` to
install the patched proxy toolchain. External ingress is upgraded separately.

See [Upgrade instructions](docs/operations/upgrades.md#upgrade-to-030-charts-and-file-exports)
for the single-user stack, explicit migration verification, Keycloak
reconciliation, ingress reload and deployments with optional warehouse drivers.

### Security

- External uploads and attachment import are disabled by default. Trusted
  authenticated deployments may opt in with `PRECIS_MCP_UPLOADS_ENABLED=true`;
  URL import also requires an exact hostname allow-list. Exports and downloads
  remain enabled. No finance writeback tools are added.
- Origin OAuth aliases identify the canonical realm issuer. Bundled Keycloak
  reconciliation repairs hidden MCP audience scopes so reported scopes retain
  the audience on refresh; reconnect clients after applying the repair.
- Artifact ingress and application logs suppress transfer capabilities.
- Refreshed compatible widget dependencies, including the MCP TypeScript SDK,
  and rebuilt all widget resources. Three remaining npm findings are confined
  to the single-file build plugin's pattern matcher; no external inline
  patterns are configured and these dependencies do not execute in the widgets.
- Updated the optional Snowflake connector constraints to 4.8.0 for
  CVE-2026-86597 and aligned shared dependencies with the hashed core lock.
- Refreshed the Python application and PostgreSQL 16 base images with patched
  Debian PCRE2 and Perl packages. Rebuilt bundled Caddy 2.11.4 with Go 1.26.9
  to fix CVE-2026-78667 and CVE-2026-97031. Update the deployment files and pull
  PostgreSQL as well as the application; source and bundled-proxy installations
  must rebuild their images. PostgreSQL stays on major version 16.
- The optional Databricks/OAuthlib CVE-2026-49265 exception remains in effect
  through 2026-10-31; these image fixes do not resolve that incompatibility.

## [0.2.7] - 2026-10-01

### Changed

- Refreshed the hashed Python lock and optional driver constraints to PyJWT
  2.15.1 and urllib3 2.8.0 after new upstream advisories.
- Updated bundled Keycloak to 26.7.5 and ClickHouse to the official 26.3.37.3
  Alpine image. The ClickHouse image changes its base distribution; its health
  probe and deploy readiness checks now use IPv4 explicitly. Existing named
  data volumes remain the deployment's data store.

### Security

- Keycloak 26.7.5 no longer contains CVE-2026-13506, CVE-2026-75595, or
  CVE-2026-8763 in the scanned runtime dependencies. Removed their temporary
  Trivy exceptions. The separate exception for the unused MSSQL JDBC driver
  remains when the bundle uses PostgreSQL.
- The optional Databricks connector still requires OAuthlib 3.x, while its
  PKCE timing advisory (CVE-2026-49265) is fixed in OAuthlib 4.0.0. The core
  published image does not install this optional driver. The optional-driver
  audit has a narrowly scoped exception through 2026-10-31 while Databricks
  compatibility is resolved; installations building with `--extras databricks`
  retain that finding.

## [0.2.6] - 2026-09-24

### Added

- Documented the existing public, read-only synthetic-data demo and how to
  connect common MCP clients. Self-hosted deployments keep their own data and
  authentication; the public demo is not a customer deployment mode.

### Changed

- Refreshed the hashed Python dependency lock, the application base-image pins,
  and the bundled PostgreSQL, ClickHouse, and Keycloak image pins. Updated the
  bundled Caddy image and its security-patched Go modules.
- Excluded nested `node_modules` directories from the application image build
  context so unrelated development packages do not ship in the runtime image.

### Security

- Keycloak moves from 26.7.2 to 26.7.4, but its server-runtime JARs still
  contain CVE-2026-13506, CVE-2026-75595, and CVE-2026-8763. This release
  accepts those residual findings until 2026-10-24 while waiting for a tested
  upstream image; it does not claim to fix them. Installations whose policy
  disallows these findings can use external OIDC instead of bundled Keycloak.

No instance schema or data migration is required for this release.

## [0.2.5] - 2026-08-28

### Added

- `precis-finance-mcp-admin ingestion run` is the host-side ingestion
  commissioning command. It supports period and snapshot bindings,
  `--stop-after extract|validate|swap`, audit notes, non-zero failure exits,
  and a summary of the persisted data-quality/reconciliation result.

### Changed

- Inspection-grid results distinguish source truncation from the smaller
  agent/widget preview through `source_truncated` and `preview_truncated`.
  Downstream integrations may attach a `data_ref` for paging the complete
  bounded extract; standalone open deployments remain cache-free and return
  the capped preview only.

### Fixed

- Base metrics configured with `aggregation: avg`, `min`, or `max` now compile
  the declared aggregate instead of being summed. Catalogue validation rejects
  the unsupported combination of a non-sum source aggregation with
  `rollup_method: avg`, preventing ambiguous double-averaging.
- Ingestion data-quality and reconciliation checks are restricted to the
  current `_load_id` and, for period bindings, the requested period. Rows left
  in staging by an earlier failed or diagnostic run can no longer affect a
  later attempt's verdict.
- Snowflake connections no longer ask Ibis to create its account-level helper
  UDF database. Read-only source credentials can connect without that DDL
  privilege.
- The base and optional-driver dependency sets now pin `cryptography 50.0.1`;
  optional drivers also pin `pyOpenSSL 26.4.0` and `pyasn1 0.6.4`. These clear
  `PYSEC-2026-3552`, `PYSEC-2026-3553`, `PYSEC-2026-3554`, and the
  `PYSEC-2026-3455`–`3457` advisory set before the release image is built.
- Bundled image pins now ship PostgreSQL 16.15 on Debian Bookworm, ClickHouse
  26.3.24.4, Keycloak 26.7.2, and a hardened Caddy 2.11.4 build. Open CI now
  scans those exact sidecar digests and the custom Caddy image for fixable
  high/critical vulnerabilities instead of scanning only the application image.

### Removed

- The exported demo `semantic.v_pnl` view no longer exposes the row-level
  `rollup_method` column; time-rollup semantics come from metric catalogue
  metadata. **Upgrade action:** remove any custom query dependency on that
  view column before reapplying the bundled semantic views.
- The duplicate `scripts/run_ingest_stage.py` entrypoint. **Upgrade action:**
  replace `python scripts/run_ingest_stage.py <flags>` with
  `precis-finance-mcp-admin ingestion run <flags>`. The binding, period,
  stage-stop, trigger-label, and notes flags are unchanged; the consolidated
  CLI defaults `triggered_by` to `cli:<OS user>` instead of `ops:manual`.

## [0.2.4] - 2026-07-13

### Added

- **Grain-aware period queries.** Period bounds can use month, quarter, fiscal
  year, week, or day codes where the selected domain declares that grain.
  Prior-period queries use declared calendar dimensions for irregular week/day
  boundaries, and average/closing rollups follow each domain's native grain.

### Changed

- **Published artifact names now match the renamed repository.** The Python
  distribution is `precis-finance-mcp`, the image is
  `ghcr.io/precis-finance/precis-finance-mcp`, and the installed admin commands
  are `precis-finance-mcp-admin` and `precis-finance-mcp-clickhouse-init`. The
  Python import namespace (`precis_mcp`), `PRECIS_MCP_TAG`, Compose project and
  service names, OAuth scope, and telemetry service name remain stable.
  **Upgrade action:** direct-install users should uninstall the Précis-built
  `precis-mcp` distribution before installing `precis-finance-mcp`; package
  managers treat them as two distributions even though both use the stable
  `precis_mcp` import namespace. Update automation that invokes the renamed
  admin commands, and pull the image from its new GHCR path.
- The MCP Registry manifest advertises the hosted public, read-only Précis demo
  as a Streamable HTTP remote. The endpoint requires no credentials and contains
  synthetic data; self-hosted deployments retain the standard OAuth-protected
  configuration.

### Fixed

- The exported demo instance no longer carries integrated-planning catalogue or
  semantic-view references that require the commercial writeback tier.
- The package-only local Compose quickstart now tracks the current release tag
  instead of pulling the older `0.2.1` image.
- The MCP Python SDK is constrained to the stable `1.x` line at `1.28.1` or
  newer, resolving `CVE-2026-59950` before the release image is published.

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
