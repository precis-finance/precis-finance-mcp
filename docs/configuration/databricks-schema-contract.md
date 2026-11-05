---
description: Required Databricks reporting views, dimension columns, scenario registry and hierarchy objects for the Précis Finance MCP native backend.
---

# What your Databricks warehouse must contain

This is the warehouse contract for the native Databricks backend of Précis
Finance MCP. It describes the objects your data team must provide, the columns
the reporting engine expects and the Précis-owned objects you provision from
generated SQL. Connection settings, credentials and Docker deployment belong
in [Analytical backends](analytical-client.md).

There is no universal finance fact-table schema to copy. Your **catalogue**
defines which reporting views, dimensions and measures are needed. Your team
maps existing warehouse data into that shape. The scenario registry and
generated hierarchy projections have package-defined shapes shared with the
ClickHouse backend. No separate schema-manifest file is required.

## Objects and ownership

| Object | Required when | Who supplies it |
|---|---|---|
| Reporting views named by domain `source_view` | For each native reporting domain | Your data team supplies the semantic SQL and maintains its inputs. |
| Dimension relations named by `source.table` | For each configured leaf dimension, including calendars | Your data team supplies keys, attributes and parent relationships. |
| `semantic.scenarios` Delta table | Every native deployment | Précis supplies the fixed DDL and seed renderer; an operator provisions it. |
| Generated node/edge views, rollup views and backing Delta tables | For configured ragged hierarchies | Précis generates them from your catalogue and hierarchy inputs; an operator provisions and refreshes them. |

Customer actuals and master-data pipelines remain yours to load, reconcile and
recover. Précis does not require its ClickHouse `live`/`staging` layout, ingestion
audit columns such as `_load_id`, or a copy of the sample model in Databricks.
Genie spaces and Databricks metric views are not required; Précis applies its own
catalogue definitions to ordinary SQL relations.

Static budgets and forecasts can be supplied through the same reporting views,
with their own registered scenario IDs. Commercial planning tables and write
workflows are outside this open-package contract.

## Logical names and physical locations

Catalogue references use a bare semantic name such as `v_finance`, or
`semantic.v_finance`. Précis resolves these to a fully qualified Unity Catalog
name. For example, with `DATABRICKS_CATALOG=finance`:

| Logical reference | Default physical name | Schema override |
|---|---|---|
| `semantic.v_finance` | `finance.semantic.v_finance` | `PRECIS_DATABRICKS_SEMANTIC_SCHEMA` |
| `semantic.scenarios` | `finance.semantic.scenarios` | Same semantic-schema override |
| `live.<object>` or `actuals.<object>` in generated SQL | `finance.actuals.<object>` | `PRECIS_DATABRICKS_ACTUALS_SCHEMA` |

Keep physical catalog/schema prefixes out of catalogue references. Native
catalogue sources must reference the semantic layer; source tables elsewhere
can be read by your semantic views. Select Databricks at deployment level,
rather than declaring a native domain as `backend_kind: ibis` (which selects
the separate federation path).

The adapter accepts identifiers matching `[A-Za-z_][A-Za-z0-9_]*`; a Databricks
name containing spaces or hyphens is not accepted as a configured catalog,
schema or catalogue object name. The serving identity also needs access to
the corresponding column metadata: native predicates and dimension joins read
types/nullability from the catalog's `information_schema.columns`.
[Unity Catalog information schema](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-information-schema)
exposes metadata according to the principal's object privileges.

## Reporting-view columns

Every native domain's `source_view` must expose the following columns as
applicable. Additional columns may exist; they are exposed to clients only
through configured metrics, dimensions or inspection columns.

| Column or catalogue reference | Requirement |
|---|---|
| `scenario` | A string matching a stored `semantic.scenarios.scenario_id`, not its alias or display name. |
| `period` | A canonical monthly string, for example `2026-01`. This column remains required for finer-grain domains. |
| `native_grain_column` | The actual time-grain column used for average/closing rollups; defaults to `period`. A daily domain, for example, also exposes its configured day column. |
| Each domain dimension's `source` | The physical key column used for that dimension's grouping/filtering. It must agree with the dimension master's key values. |
| Each base metric's `source_column` | A column compatible with its declared aggregate. Numeric aggregates require numeric values; `count_distinct` may use an identifier. A row-count metric does not require a separate measure column. |
| Each metric predicate's `where[].column` | The column used by the configured filter, with values/types matching that predicate. |
| Time-grain parents of the configured `period` dimension | Columns such as `quarter` and `fiscal_year` when those grain dimensions are declared as period parents. |
| Each `inspect_columns` entry | A reachable column for the configured inspection surface. |
| `commit_id` | Required only for a domain with `versioned: true`; it carries Précis commit semantics, not a Delta table version. Ordinary static reporting can use `versioned: false`. |

Expose detail at a known grain and avoid joins that multiply financial rows.
You may aggregate source transactions into the reporting grain, but preserve
the dimensions and native periods needed by the configured metrics. In
particular, monthly totals cannot supply correct daily closing or daily-average
behaviour merely by adding a day label. Derived metrics and statement layouts
are defined in the catalogue; they do not each need a separate view column.

### Types, nulls and financial meaning

Use stable keys, consistent types across fact/dimension joins, and canonical
period strings whose ordering matches chronology. String keys are a useful
choice for identifiers with leading zeroes. Provide one effective dimension
row per key and complete key coverage for the reporting facts; duplicate master
keys can multiply joined values. These are input-quality obligations, not
uniqueness guarantees supplied by startup validation.

Use numeric types for measures, with explicit precision suitable for your data.
For currency, prefer a fixed-scale
[`DECIMAL(p,s)`](https://docs.databricks.com/aws/en/sql/language-manual/data-types/decimal-type)
over formatted strings. The example below uses `DECIMAL(18,2)`; this is an
example choice, not a package-wide precision requirement. Boolean predicates
support `BOOLEAN` or integer flag columns; strings such as `'true'` are not
interchangeable with those types.

Preserve missing values as SQL `NULL` unless your financial definition explicitly
requires another treatment. Native SUM over no matching input returns zero;
nonempty all-null input remains null. An unregistered scenario or unknown
member remains an invalid selection. Nullable dimension attributes
retain nulls; for unmatched nonnullable attributes, the adapter supplies the
native type's default. A matched nonnullable attribute that evaluates to null
raises an error. Model an explicit unknown member when that is the intended
business meaning rather than relying on join defaults.

Expose amounts in the units expected by the catalogue and normalise signs
once. Credit-negative ledger revenue can use catalogue `sign: negate`; revenue
already made business-positive in the view uses `sign: raw`. Preserve negative
adjustments and reversals. Percentage sources use percentage points (`75`
means 75%); ratio formulas multiply by 100. Output scaling and display rounding
belong to the engine/consumer, not preformatted source strings. See
[metric definitions](catalogue-and-semantic.md#a-base-metric) for the full rules.

### Example: a monthly reporting projection

Suppose your existing `finance.curated.monthly_finance` relation contains
monthly signed amounts, including negative adjustments, and
`finance.curated.cost_centres` contains one row per cost-centre code. The
following projections adapt their column names to a small reporting model.
The catalog and schemas must already exist; these statements are examples for
your data team's SQL deployment, not operations performed by application startup.

```sql
CREATE OR REPLACE VIEW finance.semantic.v_finance AS
SELECT
    CAST(month_code AS STRING)          AS period,
    CAST(scenario_id AS STRING)         AS scenario,
    CAST(cost_centre_code AS STRING)    AS cost_centre,
    CAST(statement_line AS STRING)      AS fs_line,
    CAST(amount AS DECIMAL(18, 2))       AS amount
FROM finance.curated.monthly_finance;

CREATE OR REPLACE VIEW finance.semantic.dim_cost_centre AS
SELECT
    CAST(code AS STRING) AS cost_centre,
    CAST(name AS STRING) AS cost_centre_name
FROM finance.curated.cost_centres;
```

A matching catalogue domain uses `source_view: v_finance`, defaults
`native_grain_column` to `period`, and maps its cost-centre dimension to
`source: cost_centre`. A revenue metric can aggregate `source_column: amount`
with a predicate on `fs_line = 'Revenue'`. The dimension master uses
`source.table: dim_cost_centre`, `key_column: cost_centre` and maps its name
attribute to `cost_centre_name`. Any separately configured period/calendar
master must also be supplied. See [Catalogue & semantic model](catalogue-and-semantic.md)
for complete YAML definitions.

Rows with `scenario = 'ACTUALS'` require a registry row whose
`scenario_id` is exactly `ACTUALS`. The alias `actuals` is what clients can
request; it is not a replacement for the stored ID in fact rows.

## Dimension and calendar relations

For every leaf dimension, provide its `source.key_column`, every column named
by `source.attribute_mapping`, and every `parents.*.source_column`. Derived
dimensions reuse their declared source relationship; they do not require a
duplicate master table. Display labels and canonical keys have different jobs:
join/filter on the keys and supply labels through attribute mappings.

The native join adapter handles scalar string, Boolean, integer, floating-point,
decimal, date and timestamp attributes according to their warehouse metadata.
Use a scalar projection for complex data such as arrays or structs. Nullable
attributes may be intentional, but required IDs, period codes and relationship
coordinates should have a defined value and consistent encoding.

For a `grain: week` or `grain: date` calendar dimension, its master must also
have an integer `seq`: unique, dense and increasing in chronological order.
Prior-period retrieval joins `previous.seq = current.seq - 1`. Supply the
calendar's intended complete sequence, including periods without facts;
gaps in a fact table must not redefine which period came before another.
Use canonical `YYYY-Www` week codes or `YYYY-MM-DD` day codes, with monthly
`period` and any declared higher-grain columns carried by the fact view.
The calendar's membership determines which days/weeks are predecessors; do
not infer a week-to-month parent where that relationship is ambiguous.

## Fixed scenario registry

The renderer creates `semantic.scenarios` as a Delta table with the following
columns. **All columns must exist**, including metadata a read-only deployment
does not edit. “Nullable” describes the generated Databricks DDL; a nullable
column is not an optional column.

| Column | Databricks type | Nullable | Meaning |
|---|---|---|---|
| `scenario_id` | `STRING` | No | Stable ID present in fact `scenario` values. |
| `alias` | `STRING` | No | Unique reporting key, e.g. `actuals`. |
| `name` | `STRING` | No | Display name. |
| `base_scenario` | `STRING` | Yes | Base scenario ID, when applicable. |
| `status` | `STRING` | No | Lifecycle status. |
| `description` | `STRING` | No | Description; may be empty. |
| `created_by` | `STRING` | No | Creator identity. |
| `created_at` | `TIMESTAMP` | No | Creation time. |
| `locked_at` | `TIMESTAMP` | Yes | Lock time, when applicable. |
| `horizon_start` | `STRING` | No | Start period; may be empty. |
| `horizon_end` | `STRING` | No | End period; may be empty. |
| `actuals_cutoff` | `STRING` | Yes | Forecast metadata; does not enable commercial forecast composition in the open package. |
| `granularity` | `STRING` | No | Scenario grain metadata, e.g. `monthly`. |
| `owner_user_id` | `STRING` | No | Owner identity; may be empty. |
| `updated_at` | `TIMESTAMP` | No | Last update time. |
| `variant_of` | `STRING` | Yes | Parent variant ID, when applicable. |
| `locks` | `STRING` | No | Serialised lock metadata; empty value is `'[]'`. |
| `kind` | `STRING` | No | Scenario kind, e.g. `ACTUAL`, `BUDGET` or `FORECAST`. |

Provide at least the scenarios your model reads, with unique IDs and aliases.
Aliases must not contain `_vs_`, end in `_py`, `_pp` or `_pct`, or use the
reserved `prior_year` / `prior_period` aliases. Shifted and comparison scenarios
are generated by the registry; do not insert fact rows for keys such as
`actuals_vs_budget`.

The renderer reads `instance/scenarios.yml`. Only `scenario_id`, `alias`,
`name` and `kind` must be supplied in each YAML row:

```yaml
scenarios:
  - scenario_id: ACTUALS
    alias: actuals
    name: Actuals
    kind: ACTUAL
```

The parser fills the other fields: status `DRAFT`, creator `system`, creation
and update timestamps at parse time, granularity `monthly`, locks `'[]'`, empty
descriptive/horizon/owner fields, and null optional relationships/cutoff/lock
time. The native session and seed renderer use UTC. Defaults are supplied as
explicit seed values; the generated Delta table does not declare SQL column
defaults. A hand-written INSERT must therefore supply all nonnullable fields.

Provisioning seeds absent IDs only. It neither updates existing registry rows
nor migrates an existing table's schema. Serialise provisioning with other
scenario writers and validate IDs/aliases first; the seed MERGE does not supply
concurrent uniqueness. Customer fact pipelines must not replace this registry
as part of reloading actuals.

## Hierarchy objects, when configured

For each ragged hierarchy, the generated semantic objects use the stem
`dim_<leaf>_<hierarchy>`. `<leaf>` and `<hierarchy>` are catalogue dimension
keys. These objects support both reporting rollups and authorisation membership.

| Object | Column shape |
|---|---|
| `<stem>` node view | `node_id`, `node_name`, `display_name`, `node_type`; generated-level hierarchies additionally expose `level`, `parent_node_id`, `sort_key`. |
| `<stem>_edges` view | `child_node_id`, `parent_node_id`. |
| `<stem>_rollup_materialized` Delta table | `node_id` and the configured leaf master's key column, both projected as strings. One distinct pair per reachable node/leaf. |
| `<stem>_rollup` view | The same two columns, projected from the backing table. |

For a **generated** hierarchy, supply the leaf master and configured ancestor
columns/relationships. For a **provided** hierarchy, supply a non-leaf node
relation with `node_id`, `node_name`, `node_type`, plus an edge relation with
the configured child/parent columns. Use string node IDs matching the leaf
keys' string representation; Précis injects the leaf nodes and derives
`display_name`. Keep node IDs unambiguous across leaf and non-leaf nodes.

The shared generator guards recursive paths against cycles and deduplicates
node/leaf pairs reached through multiple paths. It requires a warehouse capable
of [recursive CTEs](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-qry-select-cte);
warehouse recursion/result bounds still apply. Use the generated projections
rather than maintaining another closure algorithm.

After initial provisioning, your pipeline must refresh mappings whenever
hierarchy inputs change. Keep affected reporting unavailable until source
publication and all mapping refreshes are complete: replacement is atomic per
backing table, not across the whole hierarchy update. Startup does not refresh
them or detect stale membership. Commands and failure handling belong in
[Hierarchy provisioning and refresh](analytical-client.md#explicit-hierarchy-provisioning-and-refresh).

## Provisioning and validation

From a source checkout of the open repository, render the package-owned
objects for your catalogue and scenario seeds:

```bash
python scripts/render_databricks_contract.py \
  --catalog finance \
  --catalogue instance/catalogue \
  --scenarios instance/scenarios.yml \
  --output build/databricks-contract.json
```

The output contains SQL **and bound seed parameters**. It creates the semantic
schema if absent, the scenario table and seeds, and configured hierarchy
objects. It does not create customer fact/dimension inputs. Rendering needs no
warehouse credentials and executes nothing. Review and apply the bundle through
your operator SQL tooling with provisioning permissions; it is generated output,
not another file the server reads. Set `--semantic-schema` when overriding the
default semantic schema, consistently with the runtime configuration.

With the native backend and serving credentials configured, check the inputs:

```bash
python scripts/check_databricks_contract.py --catalogue instance/catalogue
```

The checker uses the same validation as application startup/reload. It projects
catalogue-required columns with `SELECT ... LIMIT 0` and loads the stored
scenario registry, detecting inaccessible relations/columns, duplicate IDs or
aliases, and reserved alias syntax. It can start a stopped warehouse, but performs
no provisioning, ingestion or repair. A failed catalogue reload leaves the
previous catalogue active. Readiness checks configuration without warehouse SQL
and are not a replacement for this input check.

A passing check does **not** prove complete type/nullability compatibility,
dimension-key uniqueness or coverage, valid period contents, matching fact and
registry scenario IDs, fresh hierarchy mappings, or reconciled financial values.
Runtime queries inspect actual types for flags and dimension joins; they do not
compare every input against an independent expected schema. Your pipeline owns
input-quality checks and reconciliation against agreed control totals.

The regular open MCP serving principal needs read access to the required
objects and their metadata; provisioning and hierarchy refresh use separate
operator/pipeline permissions. See [runtime permissions](analytical-client.md#runtime-permissions).
Warehouse recovery ownership is described in
[Backups and restore](../operations/backups.md#databricks-and-customer-managed-warehouse-recovery).
