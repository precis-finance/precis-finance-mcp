---
description: Configure governed ingestion from finance source systems into the Précis Finance MCP ClickHouse read layer and validate each pipeline stage.
---

# Ingestion & data sources

Ingestion loads your actuals and master data into the ClickHouse read layer the
engine queries. If you already populate that layer yourself — your data team
materialises the `live.*` tables and `semantic.*` views — you can skip this
entirely and run [query-only](../getting-started/quickstart.md). This page is
for letting Précis Finance MCP pull from your own sources.

It applies to both [data modes](../deployment/clickhouse-data-modes.md) where
you bring your own data: a bundled ClickHouse provisioned empty
(`bundle-empty`) or your own cluster (`byo`). The `bundle-sample` mode runs
this same pipeline against generated data, so its `instance/integrations/`
files are a working reference configuration.

## What you need

- **ClickHouse** — the destination, provisioned with the `live` / `staging` /
  `semantic` databases. The provisioner creates them from your `instance/`
  directory — see the
  [ClickHouse schema contract](clickhouse-schema-contract.md) and
  [data modes](../deployment/clickhouse-data-modes.md).
- **PostgreSQL** — required. Every load writes an audit row to the
  `load_history` table, and the platform database also backs the per-binding
  advisory lock that serialises loads so two loads can't write the same table
  concurrently. No Redis or other coordination service is needed.

  Note: the single-user [quickstart](../getting-started/quickstart.md) bundle
  ships *without* PostgreSQL, so it cannot run this pipeline. To
  ingest, use the multi-user bundle (`deploy/docker-compose.yml` — both are
  included) or point the daemons at instances you already run. On the
  single-user stack you can still bring your own data by populating
  ClickHouse yourself.
- A reachable **source**: a Postgres, MSSQL, Snowflake, BigQuery, or
  Databricks warehouse, or files (CSV / Parquet / XLSX) in a drop location.
  Sources are read through [Ibis](https://ibis-project.org/). Postgres and file
  drops work out of the box; the other warehouses need their optional driver
  extra — the source factory raises naming the extra if it is absent. How you
  install it depends on how you run Précis Finance MCP:
    - **pip install** (single-user / local): `pip install 'precis-finance-mcp[snowflake]'`
      (or `[bigquery]`, `[mssql]`, `[databricks]`).
    - **Docker** (the bundled stacks): bake the driver into the image at build
      with the `PRECIS_EXTRAS` build arg — `bash scripts/deploy-mcp.sh --extras bigquery`
      (comma-separate for several: `--extras bigquery,snowflake`). The deploy
      script persists the choice in `deploy/.env` and rebuilds; the driver is
      version-pinned by `constraints-extras.txt`. `mssql` additionally pulls the
      Microsoft ODBC system driver into the image. **Adding a driver to an
      already-running instance** is the same command — re-run
      `deploy-mcp.sh --extras <name>`; Docker reuses the cached base layers, so
      only the small driver layer rebuilds before the container recreates (a few
      seconds' restart). Don't `pip install` inside a running container — there's
      deliberately no in-place path; a runtime install is lost on the next image
      rebuild.

## How a load works

```text
extract  →  validate  →  swap
   ↓            ↓           ↓
source →   staging.<x>   live.<x>
```

1. **Extract** — your SQL query runs *on the source* via Ibis (the source's
   own engine plans any joins and aggregation; only result rows traverse the
   wire) and the rows land in `staging.<table>` in ClickHouse.
2. **Validate** — the staging table's column shape is diffed against the live
   table's. A mismatch aborts before anything touches live. A zero-row
   extract also stops here: an empty staging slice will never be swapped over
   live data.
3. **Swap** — promotion is atomic. Period loads use
   `REPLACE PARTITION '<period>'` (other periods untouched); snapshot loads
   use `EXCHANGE TABLES` (metadata-only full swap). Queries never see a
   half-loaded state.

Re-running the same load replaces rather than appends — the staging slice is
cleared before each extract. Every attempt, success or failure, is recorded in
`load_history`.

## Describing a source

A **source** is one physical origin of data — a warehouse connection or a file
drop. One YAML file per source under `instance/integrations/sources/`:

```yaml
# instance/integrations/sources/customer_pg.yml
id: customer_pg
display_name: "Customer Postgres warehouse"
kind: postgres
secret_ref: customer_pg
network:
  egress_required: true
  endpoints: []
backend: {}
metadata:
  usage: "warehouse_ingestion"
```

A file-drop source reads files from a directory or upload landing area:

```yaml
# instance/integrations/sources/crm_filedrop.yml
id: crm_filedrop
display_name: "CRM CSV file drop"
kind: http_upload
secret_ref: crm_filedrop
network:
  egress_required: false
  endpoints: []
backend:
  file_format: csv
  prefix: "crm/"
  csv: { delimiter: ",", encoding: "utf-8", has_header: true, quotechar: '"' }
metadata:
  usage: "ingestion"
```

How files arrive at the drop location is up to you: copy them into the
watched volume (`docker compose cp <file> precis-mcp:/data/ingest/`, a host
bind-mount, or your own transfer job), or configure an S3 / SFTP drop store
(`PRECIS_S3_*` / `PRECIS_SFTP_*`) that an external system pushes to. The
SFTP store verifies the server's host key and refuses to start without
`PRECIS_SFTP_KNOWN_HOSTS` or `PRECIS_SFTP_HOST_KEY` — see the
[environment-variable reference](environment-variables.md#sftp-drop-folder-optional-install-the-sftp-extra-pip-install-sftp). The
`http_upload` kind name comes from the Précis platform's HTTPS upload
endpoint, which writes into the same store — the open server does not mount
that endpoint.

The same source object also serves federated reads (catalogue domains that
reference `backend: customer_pg`) — one declaration, one credential path.

### Credentials

`secret_ref` names an env-var prefix, uppercased. For `secret_ref:
customer_pg` the runtime resolves:

```bash
CUSTOMER_PG_HOST=warehouse.internal
CUSTOMER_PG_PORT=5432          # optional, default 5432
CUSTOMER_PG_USER=precis_ingest
CUSTOMER_PG_PASSWORD=…
CUSTOMER_PG_DATABASE=analytics
CUSTOMER_PG_SCHEMA=public      # optional
```

Each variable supports `*_FILE` indirection (e.g.
`CUSTOMER_PG_PASSWORD_FILE=/run/secrets/pg_password`) for Docker file-secrets.

For any warehouse that is **not** co-located with the server (reached over a
VPC or the internet), enable TLS — this link carries financial data and the
warehouse credentials:

```bash
CUSTOMER_PG_SSLMODE=verify-full          # not 'require' — that encrypts but authenticates nothing
CUSTOMER_PG_SSLROOTCERT=/etc/precis/secrets/warehouse-ca.pem
```

The suffixes above are for `kind: postgres` (and `mssql`, which adds
`_DRIVER`, default `ODBC Driver 18 for SQL Server`, and defaults `_PORT` to
`1433`). The other warehouse kinds read their own suffixes under the same
`<SECRET_REF>_*` prefix:

- **snowflake** — `_ACCOUNT`, `_USER`, `_PASSWORD`, `_DATABASE`, `_WAREHOUSE`, `_SCHEMA` (optional), `_ROLE` (optional)
- **bigquery** — `_PROJECT_ID`, `_DATASET_ID` (optional), `_CREDENTIALS_JSON` (the service-account key JSON as a value secret, via the `*_FILE` seam; omit for Application Default Credentials)
- **databricks** — `_SERVER_HOSTNAME`, `_HTTP_PATH`, `_ACCESS_TOKEN`, `_CATALOG` (optional), `_SCHEMA` (optional)

A file-drop source has no credentials to resolve; the registry logs a startup
warning about the missing env vars, which is expected and harmless there.

### BigQuery (service-account credentials)

BigQuery authenticates with a Google service account, not a username and
password, so its setup differs from the warehouses above. A worked source:

```yaml
# instance/integrations/sources/customer_bq.yml
id: customer_bq
display_name: "Customer BigQuery warehouse"
kind: bigquery
secret_ref: customer_bq
network:
  egress_required: true
  endpoints: []
backend: {}            # project / dataset come from env, not from here
metadata:
  usage: "warehouse_ingestion"
```

```bash
CUSTOMER_BQ_PROJECT_ID=my-gcp-project
CUSTOMER_BQ_DATASET_ID=finance                                  # optional default dataset
CUSTOMER_BQ_CREDENTIALS_JSON_FILE=/run/secrets/bq_sa_json       # the SA key JSON (via *_FILE)
```

The service-account key is a **value secret**, carried exactly like every
other secret — not a path the connecting process opens itself. Set
`CUSTOMER_BQ_CREDENTIALS_JSON` to the key JSON directly, or — the usual
production shape — mount the key file and point
`CUSTOMER_BQ_CREDENTIALS_JSON_FILE` at it; the platform secret loader reads the
file into `CUSTOMER_BQ_CREDENTIALS_JSON` at startup, the same `*_FILE`
convention used for `_PASSWORD` and friends (see the `*_FILE` note in the
[environment-variable reference](environment-variables.md)). The key is parsed
with `from_service_account_info`.

Omit it to fall back to Application Default Credentials
(`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default login`, or a
GCP metadata server). No `_SSLMODE` applies — BigQuery connects over
Google-managed HTTPS. Install the driver with `pip install 'precis-finance-mcp[bigquery]'`.

## Describing a binding

A **binding** ties one source to one `live.*` table and declares how loads
fire. One YAML file per binding under `instance/integrations/bindings/`,
conventionally named `<source>__<table>.yml`.

Pick the **kind** first:

- **`kind: period`** — fact data sliced by accounting period (GL postings,
  timesheets, subledgers). Each load fires for one period; the swap replaces
  exactly that partition. Periods are strings: `2026-04` for calendar months,
  plus adjustment forms `2026-13` and `2026-04-ADJ`.
- **`kind: snapshot`** — master / dimensional data with no period dimension
  (client list, employee master, CRM pipeline). Each load lands the full
  current state and atomically replaces the previous one.

If it's "the master list of X", it's snapshot. If it's
"transactions or events over time", it's period.

A period binding against a warehouse:

```yaml
# instance/integrations/bindings/customer_pg__gl.yml
id: customer_pg__gl
source: customer_pg
target: live.fact_gl
scenario: ACTUALS
kind: period

schedule:
  mode: cron
  expression: "0 6 * * *"
  timezone: "Europe/Berlin"
  period_selection:
    strategy: lookback
    lookback_periods: 3

extract:
  query: |
    SELECT
        period,
        account_code,
        cost_centre_id AS cost_centre,
        SUM(amount) AS amount
    FROM gl.journal_postings
    WHERE period = :period
    GROUP BY period, account_code, cost_centre_id
```

A snapshot binding reading a CSV from a file drop:

```yaml
# instance/integrations/bindings/crm_filedrop__fact_pipeline.yml
id: crm_filedrop__fact_pipeline
source: crm_filedrop
target: live.fact_pipeline
scenario: ACTUALS
kind: snapshot

schedule:
  mode: cron
  expression: "30 6 * * *"
  timezone: "Europe/Berlin"

extract:
  query: |
    SELECT
        opportunity_id,
        stage,
        CAST(amount AS DECIMAL(14,2)) AS amount,
        close_date
    FROM read_csv_auto('${source_path}/opportunities.csv')
```

What to know about the `extract.query`:

- It is **dialect-native SQL for the source** — Postgres SQL for a Postgres
  source, DuckDB SQL (`read_csv_auto`, …) for file drops. Ibis sends it to the
  source as-is.
- `:period` is a placeholder substituted with the scheduled period as a quoted
  literal (the period value is regex-validated first — no injection surface).
  Snapshot queries omit it.
- `${source_path}` is substituted for file-drop bindings with the resolved
  drop directory.
- **The result columns must match the target table's declared shape** — the
  column list in `instance/live/<table>.sql` (see the
  [schema contract](clickhouse-schema-contract.md)). Renames, casts, joins,
  and aggregation belong in this query (or in a view on the source side); the
  validate stage aborts the load on a shape mismatch.

The registry validates cross-references at load time: every binding must name
a known source, and at most one binding may write each `live.*` target.

## Data-quality checks

The validate stage above is structural only — it confirms the staged columns
match the live table. To gate a load on the *values* (a GL that balances,
totals that tie to the source, no orphan account codes), add a `checks:` list
to the binding. Each check runs against the staged rows **after** the
structural check and **before** the swap; a failing `error` check blocks the
swap, so bad data never reaches `live.*`.

```yaml
# instance/integrations/bindings/<source>__<dataset>.yml  (excerpt)
checks:
  # error → blocks the swap (the data is definitionally wrong)
  - name: amount_present
    type: not_null
    column: amount
    severity: error

  - name: gl_balances
    type: expression                 # a boolean predicate; failing = false
    expression: "sum(debit) = sum(credit)"
    group_by: [period, cost_centre]
    severity: error

  - name: account_mapped
    type: referential
    column: account
    references: semantic.dim_account.code
    severity: error

  - name: source_reconciles          # tie staged totals to the source
    type: reconcile
    group_by: [period, cost_centre]
    measures:
      amount_sum: { expr: "sum(amount)", tolerance: { abs: 0.01 } }
      row_count:  { expr: "count()",     tolerance: { abs: 0 } }
    source_query: |
      SELECT period, cost_centre, SUM(amount) AS amount_sum, COUNT(1) AS row_count
      FROM gl_trial_balance WHERE period = :period GROUP BY 1, 2
    severity: error

  # warning → loads, but the run is recorded as 'succeeded_with_warnings'
  - name: revenue_sign
    type: expression
    expression: "NOT (account_type = 'revenue' AND amount < 0)"
    threshold: "> 5"                 # tolerate ≤5 (reversals); warn beyond
    severity: warning

  # raw-SQL escape hatch — return the offending rows; zero rows = pass
  - name: currency_allowed
    sql: "SELECT * FROM {staging} WHERE currency NOT IN ('GBP','USD','EUR')"
    severity: error
```

**Severity** decides what a trip does: `error` blocks the swap (the load is
`failed_checks`); `warning` lets the load land but records it; `info` records
only. The default is `error`. An optional `threshold` (a comparison against
the count of failing rows, e.g. `"> 100"`) tolerates a little noise before a
check trips — absent, any failing row trips.

**Curated `type`s:** `not_null`, `unique` (`columns:`), `accepted_values`
(`values:`), `accepted_range` (`min:`/`max:`), `referential`
(`column:` + `references: schema.table.column`), `expression`
(`expression:` boolean, optional `group_by:` for aggregate predicates),
`row_count` / `distinct_count` (`min:`/`max:`), and `reconcile`. Anything the
curated set doesn't cover is a raw `sql:` check (`{staging}` / `{live}` resolve
to the binding's tables).

**`reconcile`** is the cross-system tie-out — the most important control. It
diffs each staged-side `measure` (computed by Précis over the staged rows)
against the same-named column from `source_query` (run against the source
during extract), per `group_by` group, within `tolerance` (`abs:` or `pct:`).
The `source_query` is typically a *different* table than the extract pulled
(here a trial balance), so it ties the loaded detail to the authoritative
figure; a group present on only one side is itself a failure.

Check outcomes — pass/fail, the failing count, reconcile gaps — are recorded
on the load and visible through the load-history tools alongside the status
and timestamp (see [Verifying a load](#verifying-a-load)).

## Check the configuration loads

Like every `python` command in these docs, run this inside the server
container (`docker compose exec precis-mcp …`) or in a source checkout with
the same env vars exported. Before running anything:

```bash
python -c "from precis_mcp.ingestion.registry import IntegrationRegistry; \
  IntegrationRegistry.load('instance/integrations'); print('OK')"
```

A failure means malformed YAML, a broken cross-reference, or kind-mismatched
fields — fix it before touching credentials or DDL. The config root defaults
to `instance/integrations/`; override with `PRECIS_INTEGRATIONS_ROOT`.

## Running a load

The operator script runs one `(binding, period)` through the full pipeline,
using the same code path every trigger uses:

```bash
# Full pipeline: extract → validate → swap
python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04

# Snapshot bindings take no --period
python scripts/run_ingest_stage.py --binding crm_filedrop__fact_pipeline
```

When commissioning a new binding, run one stage at a time with `--stop-after`:

```bash
python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04 --stop-after extract
python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04 --stop-after validate
```

`--stop-after extract` leaves the rows in `staging.<table>` for inspection
without touching live.

## Scheduling

Three `schedule.mode` values:

- **`cron`** — the scheduler daemon fires loads on the binding's cron
  expression. For period bindings, `period_selection` decides which periods
  each run covers (`lookback: 3` re-loads the trailing three periods, so late
  postings are picked up). In the multi-user bundle, append `ingestion` to
  `COMPOSE_PROFILES` and the daemon runs supervised:

  ```bash
  # deploy/.env: COMPOSE_PROFILES=...,ingestion
  docker compose -f deploy/docker-compose.yml up -d
  ```

  Outside compose (systemd unit, Kubernetes Deployment), run it as a
  long-lived process:

  ```bash
  python -m precis_mcp.ingestion.scheduler_daemon
  ```

- **`watch`** — the watcher daemon polls the file-drop location and fires a
  load for each new file matching `file_glob`, inferring the period from the
  filename regex or a column in the file:

  ```yaml
  schedule:
    mode: watch
    watch:
      file_glob: "gl_*.csv"
      period_from: filename_regex
      filename_regex: 'gl_(?P<period>\d{4}-\d{2})\.csv'
  ```

  In the multi-user bundle, append `ingestion-watch` to `COMPOSE_PROFILES`
  (a separate profile from the scheduler — the watcher is only useful when a
  drop store is configured). Outside compose:

  ```bash
  python -m precis_mcp.ingestion.watcher_daemon   # tick interval: PRECIS_WATCHER_INTERVAL_SECONDS, default 30
  ```

- **`push`** — an external orchestrator triggers loads over HTTP
  (`POST /api/ingest/run`). The route lives in this package
  (`precis_mcp/ingestion/run_routes.py`) but the open server does not mount
  it — it is part of the Précis platform's API host. On an open deployment,
  cron, watch, and the operator script cover the same ground.

Both daemons read the configuration and credentials at process start —
restart them after changing YAML under `instance/integrations/`
(`docker compose restart ingestion-scheduler` / `ingestion-watcher` in the
bundle). They connect to ClickHouse and Postgres with the same env vars as
the server; in the bundle the shared `x-precis-env` block in
`deploy/docker-compose.yml` is the one place source credentials are added so
they reach the server and the daemons alike.

## Verifying a load

1. **The audit row says success:**

   ```sql
   -- platform PostgreSQL
   SELECT load_id, status, rows_landed, swap_committed_at, error_message
     FROM load_history
    WHERE binding_id = 'customer_pg__gl'
    ORDER BY started_at DESC LIMIT 1;
   ```

   Terminal statuses:

   | Status | Meaning |
   |---|---|
   | `success` | Landed, validated, swapped into `live.*`. |
   | `failed_extract` | Source query failed (connectivity, SQL error, credentials). |
   | `failed_validation` | Zero rows extracted — refused before swap. |
   | `failed_recon` | Staging/live column shape mismatch — fix the extract query or the table DDL. |
   | `failed_swap` | ClickHouse swap failed. |
   | `failed_other` | Couldn't acquire the per-target lock (another load was running), or an unclassified failure. |

2. **The live table holds the expected rows:**

   ```sql
   -- ClickHouse
   SELECT count() FROM live.fact_gl WHERE period = '2026-04';
   ```

3. **The engine sees the data** — query the consuming `semantic.*` view, or
   run a metric through an MCP client. The semantic views are plain views
   over `live.*`, so no refresh step is needed.

## Related

- [ClickHouse schema contract](clickhouse-schema-contract.md) — the `live.*`
  table DDL your bindings target, and what the provisioner creates.
- [ClickHouse data modes](../deployment/clickhouse-data-modes.md) — bundled
  vs. bring-your-own ClickHouse, and provisioning either.
- [Catalogue & semantic model](catalogue-and-semantic.md) — the views that
  make loaded data queryable.
- [Quickstart](../getting-started/quickstart.md) — the query-only path if you
  skip ingestion.
