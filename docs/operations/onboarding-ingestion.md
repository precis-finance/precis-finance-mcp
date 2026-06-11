# Onboarding a data source to ingestion

How to land a new dataset into a self-hosted Précis-MCP deployment end-to-end:
declare the live table DDL, the Source and Binding YAML, wire credentials, run
a smoke load, verify. Covers both binding **kinds** (`period` for fact tables,
`snapshot` for master data) and both delivery patterns (warehouse-direct via
Ibis; file drop via the watcher).

This runbook is the task-ordered procedure. The reference detail — YAML
schema, extract queries, scheduling daemons, status tables — lives in the
[ingestion guide](../configuration/ingestion.md) and is not repeated here.

Audience: the operator or IT person wiring their company's data source into
the pipeline. Assumes write access to the deployment's `instance/` tree, shell
access to the deployment host, and the ability to set environment variables
there.

## When to run this

- Your first dataset needs to land — you've been running
  [query-only](../getting-started/quickstart.md) over data you populate
  yourself, and now want Précis-MCP to pull from the source directly.
- You're adding a dataset (you had GL only, now you're adding timesheets).
- A dataset's delivery is moving (e.g. warehouse-direct replacing a file
  drop) — same procedure with a new Source/Binding.

Do **not** run this for:

- Changing the model — metrics, statements, semantic views are a catalogue
  concern, see
  [Catalogue & semantic model](../configuration/catalogue-and-semantic.md).
- Standing up a deployment from scratch — see the
  [Quickstart](../getting-started/quickstart.md) and
  [ClickHouse data modes](../deployment/clickhouse-data-modes.md). This
  runbook assumes ClickHouse and PostgreSQL are live and provisioned
  (bundled or bring-your-own).

## Prerequisites

- **Write access** to the deployment's `instance/` tree: `instance/live/`
  (table DDL) and `instance/integrations/` (sources, bindings).
- **Source-side delivery agreed**: a reachable warehouse (Postgres, or another
  Ibis-supported backend once wired), or a file-drop location the watcher can
  poll. See [What you need](../configuration/ingestion.md#what-you-need).
- **A sample data file or query result** from the source system with realistic
  column names, types, and at least one full reporting period of rows —
  without it you can't pin the extract query's casts and renames.
- **ClickHouse DDL privilege** for the provisioner (`CREATE DATABASE` /
  `CREATE TABLE` on `live` and `staging`).
- **Platform PostgreSQL migrated** — every load writes an audit row to
  `load_history`, created by `python scripts/migrate.py --scope open` (the
  bundled compose stack runs this as its `migrate` service).
- **A scenario for the data to land under** — the binding names a
  `scenario:`; it must exist in `instance/scenarios.yml` (see the
  [schema contract](../configuration/clickhouse-schema-contract.md)).

## Steps

### 1. Pick the binding kind

- **`kind: period`** — fact data sliced by accounting period. Loads fire per
  period; the swap is `REPLACE PARTITION '<period>'`. Periods are strings:
  `2026-04`, with adjustment forms `2026-13` and `2026-04-ADJ`.
- **`kind: snapshot`** — master/dimensional data with no period dimension.
  Each load lands the full current state; the swap is `EXCHANGE TABLES`.

"The master list of X" → snapshot. "Transactions/events over time" → period.

### 2. Declare the live table DDL

Create `instance/live/<table>.sql` with the bare body — column list + engine
spec, no `CREATE TABLE` wrapper (see the
[schema contract](../configuration/clickhouse-schema-contract.md)). For period
bindings the body must carry `PARTITION BY period`; the swap requires live and
staging to share it, and the provisioner guarantees that by applying the same
body to both schemas.

Preview, then apply:

```bash
python -m precis_mcp.clickhouse_init --scope open --dry-run
python -m precis_mcp.clickhouse_init --scope open
```

Idempotent (`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW`) — safe to
re-run with other bindings live. The provisioner does **not** migrate an
existing table: column changes and `period` ↔ `snapshot` kind changes follow
[Changing a table that already exists](../configuration/clickhouse-schema-contract.md#changing-a-table-that-already-exists).

### 3. Author the Source YAML and wire credentials

Create `instance/integrations/sources/<source_id>.yml` — one Source per
physical system. Schema and worked examples (warehouse + file drop) are in
[Describing a source](../configuration/ingestion.md#describing-a-source).

Credentials resolve from env vars named by `secret_ref` uppercased
(`<REF>_HOST`, `_USER`, `_PASSWORD`, `_DATABASE`, optional `_PORT` /
`_SCHEMA`), with `*_FILE` indirection supported — see
[Credentials](../configuration/ingestion.md#credentials). For any warehouse
not co-located with the server, set `<REF>_SSLMODE=verify-full` and
`<REF>_SSLROOTCERT` — `require` encrypts but authenticates nothing.

Set the variables in the environment of **every process that touches the
source**. In the bundled compose stack that is one place: the shared
`x-precis-env` block at the top of `deploy/docker-compose.yml` (interpolated
from `deploy/.env`) — it feeds the `precis-mcp` server and the
`ingestion-scheduler` / `ingestion-watcher` daemon services alike. Outside
compose, export the same variables in whatever environment runs each daemon.

### 4. Author the Binding YAML

Create `instance/integrations/bindings/<source_id>__<table>.yml`. The binding
names the source, the `live.<table>` target, the scenario, the kind, the
schedule (`cron` / `watch`), and the **extract query** — operator-authored,
dialect-native SQL that runs *on the source* via Ibis. Full schema and
examples: [Describing a binding](../configuration/ingestion.md#describing-a-binding).

The two contracts that bite during onboarding:

- **Column shape.** The query's result columns must match
  `instance/live/<table>.sql` exactly — renames, casts, joins, and aggregation
  belong in the extract query (it executes on the source's engine, so this is
  cheap). The validate stage aborts the load on mismatch.
- **`:period`.** Period bindings filter with the `:period` placeholder
  (substituted with the regex-validated period literal). Snapshot queries omit
  it and return the full current state.

If you prefer not to embed transformation logic in Précis-MCP config, the
equivalent is a view on your warehouse and a trivial
`SELECT … FROM your_view WHERE period = :period` extract — same result,
transformation logic under your warehouse's version control instead.

### 5. Validate the configuration loads

On the deployment host — inside the server container
(`docker compose exec precis-mcp …`) or a source checkout with the same env
vars:

```bash
python -c "from precis_mcp.ingestion.registry import IntegrationRegistry; \
  IntegrationRegistry.load('instance/integrations'); print('OK')"
```

Failures mean malformed YAML, a binding naming an unknown source, two bindings
on one target, or kind-mismatched schedule fields. Fix before proceeding.
Watch the startup log for the soft warnings too — `secret_ref_missing` on a
warehouse source means step 3's env vars aren't visible to the process. (For a
file-drop source the same warning is expected and harmless — there are no
credentials to resolve.)

### 6. Make the new binding live

Registry loads are atomic — a failed reload leaves the previous configuration
in place. Two consumers hold a registry view:

- **The server** loads the registry at startup — restart it after changing
  anything under `instance/integrations/`. The restart also rebinds the Ibis
  connection cache, so federated reads pick up source or credential changes
  at the same time.
- **The watcher and scheduler daemons** (the `ingestion` /
  `ingestion-watch` compose profiles, or `python -m
  precis_mcp.ingestion.scheduler_daemon` / `watcher_daemon` outside compose)
  read the registry at process start and have no reload hook — restart them
  after any change under `instance/integrations/`:
  `docker compose restart ingestion-scheduler ingestion-watcher`. See
  [Scheduling](../configuration/ingestion.md#scheduling).

### 7. Run a smoke load

Run one `(binding, period)` through the pipeline with the operator script —
the same code path every scheduled trigger uses, so a green run here is a real
signal. Commands and flags:
[Running a load](../configuration/ingestion.md#running-a-load).

When commissioning, stage it: stop after **extract** first (rows land in
`staging.<table>` for inspection without touching live), then after
**validate** (adds the shape check), then run the full pipeline including the
swap. Snapshot bindings take no period.

For watch-mode bindings, the end-to-end equivalent is dropping a correctly
named file into the watched location and letting the watcher daemon pick it up
on its next tick (`PRECIS_WATCHER_INTERVAL_SECONDS`, default 30); the period
is inferred from the filename regex or a file column.

## Verification

Run all three checks; failure on any means the onboarding is not done.

1. **The audit row says success** (platform PostgreSQL):

   ```sql
   SELECT load_id, status, rows_landed, swap_committed_at, error_message
     FROM load_history
    WHERE binding_id = 'customer_pg__gl'
    ORDER BY started_at DESC LIMIT 1;
   ```

   `status = 'success'`, `swap_committed_at` non-null, `error_message` null.
   The failure buckets (`failed_extract`, `failed_validation` for zero-row
   extracts, `failed_recon` for shape mismatch, `failed_swap`, `failed_other`
   for lock conflicts) are explained in
   [Verifying a load](../configuration/ingestion.md#verifying-a-load).

2. **The live table holds the expected rows** (ClickHouse):

   ```sql
   SELECT count() FROM live.fact_gl WHERE period = '2026-04';
   -- snapshot bindings: SELECT count() FROM live.<table>;
   ```

3. **The engine sees the data** — query the consuming `semantic.*` view, or
   run a metric the new dataset feeds through an MCP client. The ingestion
   status tools (`list_load_history`, `get_load_status`, `list_bindings`,
   `get_binding` — see the
   [MCP tool reference](../reference/mcp-tools.md#ingestion-status)) let you
   check the same facts from the client directly.

## Rollback or recovery

Reversible up to the point of user-visible reads; each item is independent.

- **Wrong column shape discovered at smoke load** (`failed_recon`) — fix the
  extract query, or fix `instance/live/<table>.sql` and re-apply per the
  schema contract's
  [existing-table procedure](../configuration/clickhouse-schema-contract.md#changing-a-table-that-already-exists).
  Re-running the load is idempotent: the staging slice is cleared before each
  extract, and the swap replaces rather than appends.
- **Wrong kind chosen** — edit the binding's `kind`, drop and re-apply both
  tables per the same
  [existing-table procedure](../configuration/clickhouse-schema-contract.md#changing-a-table-that-already-exists),
  reload (step 6), re-run.
- **Registry won't load after an edit** — the previous registry stays active
  (atomic reload); nothing is broken while you fix the YAML.
- **Smoke-test rows cluttering `load_history`** — operator-script runs are
  labelled `ops:manual` in `triggered_by` (override with `--triggered-by`).
  Clear test rows only:

  ```sql
  DELETE FROM load_history
   WHERE binding_id = 'customer_pg__gl' AND triggered_by LIKE 'ops:%';
  ```

  Never clear production rows — `load_history` is the audit log.

## Related documents

- [Ingestion & data sources](../configuration/ingestion.md) — the reference
  guide: YAML schema, extract queries, scheduling daemons, status reference.
- [ClickHouse data modes](../deployment/clickhouse-data-modes.md) — bundled
  vs. bring-your-own ClickHouse and the schema provisioner.
- [ClickHouse schema contract](../configuration/clickhouse-schema-contract.md)
  — what `instance/live/*.sql` must contain, and the scenario registry.
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md) —
  making loaded data queryable through metrics and statements.
