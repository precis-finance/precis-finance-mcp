# Native analytical backends: ClickHouse and Databricks

The shared `precis_mcp.analytical` interface provides lazy ClickHouse and native
Databricks SQL connections. Main reporting, inspection, hierarchy discovery,
scenario permission reads and MCP instructions now select the configured client.
ClickHouse remains the default. Native Databricks reporting is implemented and
accepted on the reference deployment. Native startup requires
validated customer inputs and a stored Précis scenario registry.
Commercial planning workflows are outside the open package; basic
SQL execution does not add planning tools to the external MCP surface.

For required reporting-view columns, dimension keys, scenario-table types and
hierarchy inputs, see the [Databricks schema contract](databricks-schema-contract.md).

Install the optional native driver with
`pip install 'precis-finance-mcp[databricks-primary]'` from a build containing
this increment. The existing `databricks` extra remains Ibis federation.
Both image definitions accept `PRECIS_EXTRAS=databricks-primary` to install
the native dependencies; this is not an integrated backend deployment profile.
ClickHouse-only installs do not need the new dependencies.

## Backend choice and indicative timings

For interactive reporting, local ClickHouse can return a small financial report
in well under a second at the tool level. Databricks can take several seconds
to tens of seconds for the same report, even with an already-running warehouse.
These are observed orders of magnitude from the development deployment, not
service commitments or limits of either database.

The following examples were observed on **2026-10-25** with the same demo data
and report selections: a P&L with 19 displayed lines, five scenario/comparison
columns and six financial SQL statements. All displayed results matched.
ClickHouse ran on the application host; Databricks was a remote SQL warehouse.
The Databricks observations used connection reuse and up to three concurrent
financial queries. The ClickHouse observation preceded those changes and its
hierarchy materialization. This is a small single-user sample from different
implementation stages, not a controlled comparison or a fixed slowdown ratio.

| Measurement | Local ClickHouse, one observed request | Databricks, first / repeated report |
|---|---:|---:|
| Sum of database-reported durations for six financial SQL statements | 0.37 s | 37.2 / 11.2 s |
| Elapsed window covering those financial statements | Not separately recorded | 14.3 / 4.0 s |
| Complete `run_statement` tool call | 0.43 s | 16.0 / 4.6 s |
| Report payload emitted, measured from agent-turn start | 3.9 s | 20.0 / 6.8 s |
| Complete agent turn, including model calls | 7.7 s | 24.6 / 9.1 s |

Database-reported statement durations include compilation and execution; they
are not pure computation time. Concurrent statements overlap, so their summed
durations can exceed elapsed wall time and must not be read as CPU usage or
compute cost. Tool time additionally includes metadata reads, query submission,
result fetching and report assembly. Agent-turn time includes model calls;
payload emission does not measure browser rendering or authentication before
the trace starts. An open MCP consumer's own agent adds its own latency, so the
agent-turn examples are illustrative rather than intrinsic MCP response times.

The first Databricks report had no financial-query result-cache hits; the repeat
hit the result cache for all six statements. The warehouse was already running,
and no provisioning or overload queue time was recorded: **first does not mean
cold warehouse** here. A stopped warehouse can add startup time. Warehouse size,
region, data volume, query shape, cache state and concurrent demand can change
these timings substantially; these observations do not establish production
capacity.

Choose local ClickHouse when low interactive latency is a priority and operating
the bundled database fits the deployment. Databricks lets an organization keep
its existing warehouse and customer-managed pipelines, at the cost of higher
observed latency for these small reports. Evaluate representative first and
repeat reports on the intended deployment, measuring both tool wall time and
database statement timings; SQL execution alone does not describe the user's
wait. Commercial planning/write timings are outside this open-package example.

## Docker deployment with Databricks

The open multi-user bundle supports an explicit provider overlay. Use Docker
Compose **2.24.4 or newer**: the overlay replaces provider-specific profiles
using the documented
[`!override` merge rule](https://docs.docker.com/reference/compose-file/merge/#replace-value).
The single-user ClickHouse demo remains unchanged.

Use a source export/build containing this implementation; changing runtime
variables does not install drivers in an existing published core image. Build
with `PRECIS_EXTRAS=databricks-primary`. The `databricks` extra remains the
separate Ibis federation option.

Configure `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_CLIENT_ID`,
`DATABRICKS_CATALOG` and `DATABRICKS_CLIENT_SECRET_FILE` in the deployment
environment. The last is an **absolute host path** to the OAuth secret file.
Keep that file outside the Docker build context or inside the Docker-ignored
`secrets/` directory; arbitrary files under the checkout can be copied into an image.
Local [Compose file-secret mounts](https://docs.docker.com/reference/compose-file/services/#secrets)
preserve host ownership/permissions: make it
readable by the application's numeric **UID 10001**, with restricted permissions;
a root-owned mode-0600 file is unreadable by that application. Compose mounts
the file read-only into the serving container. No CLI profile or secret value
goes into the image or catalogue. Retain the usual PostgreSQL, identity,
artifact-signing and public-origin configuration.

From the exported repository root:

```bash
docker compose --env-file deploy/.env \
  -f deploy/docker-compose.yml -f deploy/docker-compose.databricks.yml \
  up -d --build
```

Configure the usual auth/ingress profiles. The overlay selects `databricks`
and `customer_managed`, excludes bundled CH and ingestion daemons, and mounts
the M2M secret only into the serving application. PostgreSQL migrations still
run. The warehouse, semantic inputs, scenarios, hierarchy projections and grants
must already exist; startup validates them without provisioning source data.
ClickHouse credentials are not required. Selecting an overlay does not stop
already-running CH/ingestion containers from an earlier deployment; stop those
old services explicitly during an accepted cutover, retaining their volumes.

The remote helper supports `scripts/deploy-mcp.sh --data-mode databricks`.
It selects the overlay, retains configured optional drivers while adding
`databricks-primary`, builds the image and skips CH provisioning. Repeat the
data-mode flag on later deployments; omitting it on a configured Databricks
deployment is rejected. `--data-only` cannot provision a customer-managed
warehouse. Configure the M2M file/settings on the target before invoking it.
Existing CH deployment presets retain their behavior.

Backups use the same backend selector and require no warehouse credentials.
The optional `backup` profile uses UID 10001 for the Databricks sidecar, allowing
private managed-file reads without changing artifact permissions. See
[backup ownership](../operations/backups.md#databricks-and-customer-managed-warehouse-recovery).
Configuration rendering and component checks establish neither image nor
deployment acceptance; verify the chosen image and platform restore on the
target before relying on its recovery coverage.

## Credentials and configuration

Production uses explicit service-principal OAuth M2M:

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` | HTTPS workspace origin, without a path, query or credentials |
| `DATABRICKS_HTTP_PATH` | `/sql/1.0/warehouses/<warehouse-id>` |
| `DATABRICKS_AUTH_TYPE` | Defaults to `oauth-m2m`; developer CLI access requires explicit `databricks-cli` |
| `DATABRICKS_CLIENT_ID` | Service-principal application ID for M2M |
| `DATABRICKS_CLIENT_SECRET` | Service-principal OAuth secret for M2M |
| `DATABRICKS_CLIENT_SECRET_FILE` | Optional mounted secret, resolved by the existing shared startup loader |
| `DATABRICKS_CONFIG_PROFILE` | Optional named profile for explicit developer CLI authentication only |
| `PRECIS_ANALYTICAL_BACKEND` | Deployment's primary analytical backend: `clickhouse` (default) or `databricks` |
| `PRECIS_INGESTION_MODE` | `managed` (CH default) or `customer_managed` (Databricks default and currently required there) |
| `DATABRICKS_CATALOG` | Catalog for native shared-engine SQL |
| `PRECIS_DATABRICKS_SEMANTIC_SCHEMA` | Physical schema for logical `semantic` objects; default `semantic` |
| `PRECIS_DATABRICKS_PLANNING_SCHEMA` | Physical schema for logical `planning` objects; default `precis_planning` |
| `PRECIS_DATABRICKS_ACTUALS_SCHEMA` | Physical schema for logical `live`/`actuals` objects; default `actuals` |
| `PRECIS_DATABRICKS_SOCKET_TIMEOUT` | Positive per-RPC socket timeout in seconds; default 30, maximum 300 |
| `PRECIS_DATABRICKS_STATEMENT_TIMEOUT` | Server SQL execution timeout in seconds; default 60, range 1–172800 |

Secrets follow the same plain-environment, `*_FILE` and operator-vault-wrapper
conventions as other platform credentials. The factory activates the shared
startup loader before reading settings; it does not load a dotenv file itself.
Plain values take precedence over mounted files. Restart after file rotation.
The M2M path requires both values and does not fall back to CLI credentials.
It requests the `sql` OAuth scope explicitly.

For local development, explicitly select `databricks-cli` and authenticate the
Databricks CLI beforehand. The executable must be on `PATH`. No password or
OAuth token needs to be added to `.env`; the SDK obtains refreshed credentials
through the CLI for the configured workspace. This mode is not for service
deployment. Do not enable verbose SDK/connector logging on credential-bearing
production traffic: those libraries own their diagnostic output.
The native client redacts URLs from CloudFetch download warnings and exception
text while retaining their operational context. Signed result-download links
must be treated as temporary credentials.

### Create service credentials for a native server

In workspace **Settings → Identity and access → Service principals**, create
a dedicated principal for the Précis deployment. Databricks assigns its
application/client ID. Open its **Secrets** tab and generate an OAuth secret
restricted to the `sql` API scope. Store its value when it is displayed; it is
shown only once. See [Databricks OAuth setup](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m).

Grant the reporting access described below. A valid OAuth secret does not grant
warehouse or data access, and creating a principal grants no Précis user role.

On the native host, as the account running Précis:

```bash
install -d -m 700 ~/.config/precis/secrets
umask 077
nano ~/.config/precis/secrets/databricks_client_secret
chmod 600 ~/.config/precis/secrets/databricks_client_secret
```

The file contains only the OAuth secret. Set `DATABRICKS_CLIENT_ID` and
`DATABRICKS_CLIENT_SECRET_FILE` in the process environment/configuration,
using an absolute path to that file. The runtime account must be able to read
it. A developer CLI login on another machine is not server authentication.
Keep the backend selector unchanged until the warehouse contracts, optional
dependencies and deployment assembly have been accepted. Credentials alone
do not provision the warehouse objects or establish deployment readiness.

### Runtime permissions

The standalone open MCP serving principal needs workspace access, the applicable Databricks
SQL entitlement and **CAN USE** on the selected SQL warehouse. It also needs
`USE CATALOG` on the configured catalog, `USE SCHEMA` on each accessed schema,
and `SELECT` on required semantic views/tables, including `semantic.scenarios`.
Warehouse access and Unity Catalog data privileges are separate; see
[warehouse ACLs](https://docs.databricks.com/aws/en/security/auth/access-control#sql-warehouse-acls)
and [Unity Catalog access](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/permissions-concepts).

Derive the relation list from the instance's native domain views, dimension
masters, hierarchy projections and scenario registry. Resolve their physical
names through `DATABRICKS_CATALOG` and the configured schema roles. An omitted
domain/hierarchy does not need grants merely because another instance uses it.
Access to a view does not establish direct access to its base tables; the view's
owner/dependencies must remain valid. Keep the serving principal read-only.

Operator provisioning and customer-pipeline hierarchy refresh run with separate
credentials. Creation needs namespace usage, source `SELECT` and `CREATE TABLE`
on target schemas; creating a schema also needs `CREATE SCHEMA` on its catalog.
Use an authorized owner of the Précis-owned targets for replacement operations.
Scenario seeding requires target read/write access. These are operator powers,
not additional requirements for ordinary reporting. Recheck serving grants
after view replacement. The renderer itself needs no warehouse credentials.

## Interface and limits

Use `get_analytical_client("databricks")` as a context manager. The default
provider is ClickHouse; `PRECIS_ANALYTICAL_BACKEND` selects the shared reporting
and permission-read path. The returned object has `query(sql, parameters)`, `execute(sql, parameters)` and `close()`.
`DatabricksConfig` may also be supplied explicitly, with credential fields
excluded from its representation.

SQL is trusted compiler/operator input, not an arbitrary SQL tool exposed to
agents. Databricks SQL uses native `:name` mapping parameters or `?` positional
parameters. ClickHouse retains its typed named markers. SQL dialect translation
is not implemented by the client: the shared engine constructs SQL through
`SqlDialect` helpers. Basic `execute` is trusted low-level SQL transport,
not an agent tool or an application mutation framework. Native execution accepts
success only after the connector confirms `SUCCEEDED`; `StatementError` reports
failed/cancelled/unknown/not-submitted feedback and an available statement ID.
Précis does not resubmit failed commands. Unknown outcomes require reconciliation
before retrying. ClickHouse retains its existing driver's internal transport
behaviour; this wrapper does not establish exactly-once execution. Results provide `column_names`, `result_rows`, provider-native
`column_types` and an optional query ID, preserving native Python values.

The factory returns a request-owned handle backed by a lazy process-local pool
of at most three native clients for matching connection settings. Queries borrow
a client exclusively through fetching and cursor cleanup; context exit closes
the request handle while healthy sessions remain reusable. Checkout waits at
most 30 seconds; idle sessions expire lazily after five minutes. Databricks reuses
the SDK credential provider and token renewal, with fresh cursors and UTC session
time. CH retains its shared HTTP pool and distinct sessions for concurrent loans.
Warehouse type metadata remains request-scoped. Directly constructed native
clients and operator provisioning retain their independent session lifecycle.

The retriever submits independent native financial queries across scenarios to
three shared workers and merges results in plan order. The pool cap applies
across reports in the process; multiple processes each own a pool. No per-user
or distributed SQL scheduler is introduced. Worker trace context is explicitly
copied. Failed sessions are discarded without silent SQL replay; a required
query failure fails the whole report after pending cancellation and worker drain.
Databricks `cancel()` targets only that handle's active reads; its return does
not prove terminal execution on the server. A connection is never returned while
its query/cursor is still in use. Socket, checkout and SQL execution timeouts do
not constitute a total request deadline. No query keepalive prevents warehouse
auto-stop. Server lifespan shutdown drains workers and closes pooled resources;
one-off users can call `close_analytical_resources()` explicitly.

The repository's `scripts/check_analytical_client.py --catalog <catalog>` checks
bound values, native types and an existing semantic view using reads only.
It may automatically start a stopped warehouse. It does not demonstrate
integrated finance-engine parity, concurrency capacity or production M2M grants.

## Shared-engine reads and SQL rules

The common engine accepts an injected native client without a second catalogue,
resolver, comparison policy or formatter. Its SQL helpers qualify logical
`role.table` references, bind list elements separately and preserve grouping
masks with native `GROUPING_ID`. Filter/scope and scenario metadata read
builders use the same seam. Stored report refresh also replays the existing
financial tools through their permission gate instead of reconstructing an
independent engine request. Trusted low-level statement execution does not add
commercial planning workflows to the open package.

No extra read-contract file is required. The existing catalogue supplies the
logical names and required columns; startup and explicit reload project those
columns with `LIMIT 0` and validate stored scenario vocabulary. A failed reload
retains the previous catalogue. These checks may start compute but perform no
DDL, seeding or ingestion. They do not compare the warehouse with an external
expected-schema declaration.

The SQL adapter applies one SUM rule on both backends: no matching rows returns
zero; a nonempty group containing only null values returns null. In particular,
the former CH nullable-empty SUM result is normalized to zero. Average/min/max
and division-by-zero semantics are unchanged. The native driver preserves
Decimal values without float conversion. Integer flags and Boolean columns use
their actual warehouse types. Nullable dimension attributes remain null;
unmatched nonnullable members use the type default. Those lookups use a small
cache owned by each client, discarded with that client. No operator metadata
registry or backend configuration snapshot framework is involved.

Native readiness reports the `databricks_config` check without issuing SQL;
it does not claim warehouse reachability. Backend and credential changes take
effect on restart, using the existing environment/secret conventions. The
removed prototype's `PRECIS_ANALYTICAL_READ_CONTRACT` setting is no longer read.

Customer-managed mode omits Précis ingestion tools and upload/load/configuration
routes on either backend. External Ibis source declarations remain separate.
Legacy direct CH calls are rejected in a Databricks deployment; they cannot
silently use another financial store. Explicit operator CH commissioning via
the analytical factory remains possible.

## Précis-owned scenario and hierarchy objects

Scenario metadata reads share an in-process registry cache on both backends.
`PRECIS_SCENARIO_REGISTRY_TTL` defaults to 60 seconds. Refresh is on demand after
expiry, with no expired fallback on failure; scenario mutations and catalogue
reload invalidate locally. Other processes and external updates rely on expiry.
Statement/metric tools use one registry snapshot across all columns and engine
execution. The cache contains metadata, not caller permissions or financial
results: user/profile grants remain fresh PostgreSQL reads and scenario discovery
still filters visibility per caller. Write-target resolution, status/lock
governance checks and startup/reload validation retain direct database reads.

`semantic.scenarios` remains the same logical Précis registry. Its column
definition is shared by CH and native renderers; PY/variance scenarios remain
generated by `ScenarioRegistry`. Customer actuals/master-data pipelines do not
own scenario lifecycle policy. The open MCP surface exposes scenario reads;
operators provision the stored registry separately.

`scripts/render_databricks_contract.py --catalog <catalog> --output <bundle.json>`
renders a local operator bundle using the configured catalogue and scenario
seed file. It contains the Précis schema/table DDL, bound seed-if-absent MERGE
and materialized hierarchy mappings with stable projection views. It requires no warehouse credentials and applies
nothing. Review it, validate existing/proposed aliases and IDs, and serialize
provisioning with scenario writers before applying with provisioning credentials.
MERGE does not guarantee uniqueness under competing writers. Existing scenario
rows are never updated by seed SQL; seeds supply every column and do not depend
on omitted-column defaults.
Customer actuals and master-data tables/views are not created or repaired.
`scripts/check_databricks_contract.py` runs the same read-only validator as
startup against the configured endpoint and catalogue, including stored
scenarios; it performs no DDL, seeding or ingestion.

Generated and provided hierarchy projections share one closure algorithm,
including cycle guards and diamond deduplication. Refresh computes the closure
into `semantic.dim_<leaf>_<hierarchy>_rollup_materialized`; the existing
`*_rollup` view reads those stored pairs, so reporting and scope resolution do
not repeat the recursive walk. ClickHouse uses MergeTree storage and Databricks
uses Delta. Refresh SQL uses recursive CTEs; the warehouse must support them.
Databricks's default
recursion and result-size bounds can reject oversized hierarchies; this
increment does not silently truncate them or claim unbounded closure support.

### Explicit hierarchy provisioning and refresh

The semantic namespace and compatible customer-owned dimension, node and edge
input relations must already exist. `--provision` installs only Précis-owned
hierarchy objects; it does not create the namespace or ingest customer inputs.

On either backend, the host-side operator command uses the existing selected
backend configuration and `*_FILE` secret loading. Run it in an operator/pipeline
environment with source-read and hierarchy-provisioning permissions. The regular
reporting identity and Précis user profiles do not need refresh privileges.
Customer-managed refresh credentials are separate from the reporting service;
managed CH ingestion continues to use its existing write-capable ingestion client.

```bash
# First installation, migration from recursive rollup views, or catalogue hierarchy-definition changes:
python -m precis_mcp.admin_cli hierarchy refresh --provision

# After changing hierarchy leaf data or parent-child relationships:
python -m precis_mcp.admin_cli hierarchy refresh
```

`--catalogue <directory>` overrides `PRECIS_CATALOGUE_ROOT`; otherwise the CLI
uses the existing instance-directory resolver. Initial provisioning installs
the edge/node views, computes the backing tables and switches the canonical
rollup views to those tables. Normal refresh changes backing tables only.
Programmatic callers can use
`analytical.provisioning.refresh_hierarchies(catalogue, execute, dialect)` with
their own operator connection; `provision=True` installs definitions as well.
The function stops at the first error and performs no automatic retry.

For Databricks, adding `--hierarchy-refresh-only` to the existing render script
produces only the table-replacement SQL for a customer pipeline, without needing
a scenario seed file. The bundle is generated SQL, not an additional maintained
instance file. Its statements can instead be executed by the customer's own SQL
runner. Application startup, readiness and catalogue reload never apply this DDL.

**Customer-managed publication contract (CH and Databricks).** The customer must
invoke refresh after changing leaf IDs, hierarchy ancestor columns or provided
edges; initial setup, hierarchy-definition changes and recovery also require
provisioning/rebuild before serving the affected reports. No scheduled refresh
or change-detection service is installed. Serialize hierarchy updates and keep
affected reporting unavailable during source changes and mapping publication.
Replacement is atomic for one backing table, not across source tables and all
hierarchies. Failed refresh retains that table's previous version, but earlier
source updates or mapping replacements may already be visible. Keep reporting
unavailable until those states are reconciled. Stale membership can affect
authorization scopes; retaining old mappings is not a safe availability fallback.
Précis does not automatically enforce the maintenance window or detect external
source/closure drift.

Managed CH refreshes all catalogue hierarchies after snapshot ingestion and
before marking the load successful. Hierarchy master/edge inputs are expected
to use snapshot bindings; period fact loads do not trigger refresh. A refresh
failure marks the load `failed_swap`, retaining the source-swap timestamp when
publication already happened; it does not roll back the source. The same
reporting-quiescence requirement applies. Direct writes outside ingestion must
invoke the explicit refresh. Rebuild these derived mappings after recovery when
their sources or catalogue definitions have changed.

The per-table replacement choice follows
[ClickHouse REPLACE TABLE](https://clickhouse.com/docs/reference/statements/create/table/replace-table)
(Atomic/Replicated databases) and
[Databricks table replacement](https://docs.databricks.com/aws/en/tables/operations/drop-table).
Neither provides a transaction covering this whole publication workflow.

Embedding applications that register `register_scenario_query_extension` retain
their existing two-argument SQL factory on CH. A factory used with a native
Databricks client must additionally accept the `dialect` keyword and construct
its markers through that dialect; opaque SQL strings are never translated.

`scripts/check_databricks_engine.py` is a read-only shared-engine diagnostic
requiring the reference-fixture contract: `full_pnl`, the `gl` inspection domain,
the named revenue/cost/margin metrics, actuals, division/period breakdowns and
cost-centre scopes. The operator supplies compatible catalogue/scenario fixtures;
it is not a general customer-catalogue validator. It
checks an actuals/PY/YoY statement, two-axis grains, independent SQL control
totals, empty scopes and synthetic null/default contract violations. It does
not test application authentication, stored scenario governance or commercial
writes, and its total duration is not a performance benchmark.
Optional `--benchmark-runs` separately measures warmed shared-report calls,
including all SQL reads performed inside `execute_report`. Catalogue, registry
are preloaded; MCP/auth, agent and consumer rendering are
excluded, and caches are uncontrolled. These are pipeline timings, not complete
product latency or a concurrency envelope.
