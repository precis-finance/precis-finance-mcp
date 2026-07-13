---
description: Understand how Précis Finance MCP combines governed semantic views, a YAML finance catalogue, read-only tools, and identity controls.
---

# How Précis Finance MCP works

One page on the moving parts and how they fit, before the per-topic guides go
deep. The mental model: **you bring data and a description of your financial
model; the server turns MCP tool calls into governed, read-only queries
against them.** Everything else is detail on one of those three clauses.

```text
 ┌┄┄┄ your warehouse / file drops
 ┆          │
 ┆          │  ingestion: extract → validate → atomic swap      instance/   (your model)
 ┆          ▼                                                   ├─ live/          table DDL
 ┆     ClickHouse                                               ├─ semantic/      SQL views
 ┆     ┌──────────────────────────────┐                         ├─ catalogue/     metrics YAML
 ┆     │ live.*      your data        │  provisioned from ─────►├─ integrations/  sources+bindings
 ┆     │ semantic.*  your meaning     │                         └─ scenarios.yml  dataset registry
 ┆     └──────────────▲───────────────┘
 ┆ federated read     │  reads semantic.* only
 ┆ (long-tail detail, │
 ┆  queried in place) │
 └┄┄┄┄┄┄┄┄┄┄┄┄► metric engine  ◄── catalogue (what exists, how it's computed)
                      ▲
                read-only tools  (run_statement, run_metric, inspect_rows, discovery)
                      ▲
       ┌──────────────┴───────────────┐
       │ /mcp — OAuth 2.1, multi-user │   PostgreSQL: users, profiles,
       │ /mcp — dev key, single-user  │   load audit, load locks
       └──────────────▲───────────────┘
                  MCP client (Claude, ChatGPT, your agent)
```

## The instance directory — your model as configuration

Everything specific to *your* business lives in one directory, `instance/`,
separate from the installed package: the table DDL (`live/`), the SQL views
that define what your data means (`semantic/`), the YAML catalogue of
metrics, dimensions, and statements (`catalogue/`), the ingestion
configuration (`integrations/`), and the dataset registry
(`scenarios.yml`). The repository ships a complete demo instance showing the
shape; deployments mount their own over it. If you remember one thing: **code
is the engine, `instance/` is the model** — adapting Précis Finance MCP to your
business means editing `instance/`, not Python.

## ClickHouse — the read layer

All querying happens against ClickHouse, in three databases the provisioner
creates from your `instance/`: `live` (your data), `staging` (ingestion's
landing zone, swapped atomically into `live`), and `semantic` (the views the
engine reads — its *only* query surface). You can run the bundled ClickHouse
or bring your own cluster, empty or with demo data —
[data modes](../deployment/clickhouse-data-modes.md) — and the
[schema contract](../configuration/clickhouse-schema-contract.md) defines
exactly what a ready cluster contains.

## Semantic layer and catalogue — meaning, then metrics

Your model is described in two deliberate layers. The **semantic layer**
(SQL views) says what the data *means* — what a P&L row is, which accounts
are revenue. The **catalogue** (YAML) *defines* the metrics — and the
statements and dimensions built on them — over those views: what each one
is, how it's computed (aggregation, derived formulas), and how it's
formatted. That definition is also the contract clients query against, so a
metric exists for them only once it's catalogued. The same name must appear
in both (view column → catalogue reference → field the client receives), and
the engine validates the whole chain at startup, refusing to serve an
inconsistent model. The walkthrough is
[Catalogue & semantic model](../configuration/catalogue-and-semantic.md); the
change contract is
[Adding metrics & dimensions](../configuration/adding-metrics-and-dimensions.md).

## The metric engine

Every reporting call runs one pipeline: resolve the request against the
catalogue (expand statements into metrics, derived metrics into their
inputs), apply the caller's permission scope, retrieve (ClickHouse SQL
against `semantic.*` — or Ibis expressions for a federated domain that reads
a warehouse table in place), then transform and format. Because every client
goes through the same catalogue definitions, two people asking the same
question get the same number.

## Federated reads — the long tail stays in your warehouse

The intended split: the datasets that answer most questions — the GL, the
aggregated facts your statements read — are **landed in ClickHouse**, where
the engine has its full vocabulary: every aggregation, `avg` and `closing`
rollups, multi-grain totals, hierarchy filters. For the long tail — very
detailed, rarely-queried data that is too large or too governance-sensitive
to copy (a revenue subledger, raw worklogs) — a catalogue domain can declare
`backend_kind: ibis`, and the engine queries the table **in place on your
warehouse**, through the same `Source` declaration (and credentials) that
ingestion uses.

The trade is deliberate, and the catalogue validator enforces it:

- **Sum only.** Federated metrics must be `aggregation: sum` with
  `rollup_method: sum`; totals are rolled up additively from one detail
  read. `avg` and `closing` need the data landed.
- **Filters only on modelled dimensions.** Filters and security scope
  resolve against the master data in ClickHouse and are applied to the
  foreign table as `IN (…)` predicates — so the foreign view must expose
  the native dimension key columns. Columns that exist only on the source
  can be exposed as `source_inline: true` axes: group-by only, never
  filterable or scopable.
- **Never a cross-store join.** One query against one backend per request;
  master data stays canonical in ClickHouse.
- **Read-only**, and always `versioned: false`.

When a long-tail dataset starts carrying mainline reporting, the move is to
land it: the `Source` already exists, so it's one binding and a DDL file —
and the engine's full functionality applies. The how-to is in
[Adding metrics & dimensions](../configuration/adding-metrics-and-dimensions.md#adding-federated-source-only-axes).

## Scenarios — which dataset a number comes from

A **scenario** identifies the dataset behind a figure — actuals, a budget, a
forecast. It's a column in your data, registered in `instance/scenarios.yml`
with a `kind` (`ACTUAL` / `BUDGET` / `FORECAST`); clients pick one or compare
several at query time, and the engine adds a comparison vocabulary (variance,
shifted views) on top. Scenarios are data, not catalogue entities — adding a
forecast means loading rows that carry its id and seeding its registry row.

## Ingestion — optional, and atomic

If your data team already populates ClickHouse, you can skip ingestion
entirely and run query-only. Otherwise, you declare **sources** (a warehouse
connection or file drop) and **bindings** (one source → one `live.*` table,
with an extract query and a schedule), and every load runs extract →
validate → swap: rows land in `staging`, the column shape is checked, and
promotion into `live` is atomic — queries never see a half-loaded state.
PostgreSQL keeps the audit trail (`load_history`) and serialises
concurrent loads (a per-binding advisory lock). Reference: [Ingestion](../configuration/ingestion.md);
procedure: [Onboarding a data source](../operations/onboarding-ingestion.md).

## Transport and identity — who may read what

Two entrypoints serve the same tools: a localhost, single-user **dev server**
(static key, three deliberate gates — the [quickstart](quickstart.md)) and
the multi-user **`/mcp` endpoint** (OAuth 2.1 + PKCE). Sign-in posture is one
variable, `PRECIS_AUTH_MODE`: a dev key (**A**), the bundled Keycloak,
optionally [brokered to your corporate IdP](../deployment/keycloak-brokering.md)
(**B**), or your own OIDC IdP trusted directly (**C**) —
[Remote access](../deployment/oauth-keycloak.md). Authentication only
identifies: what a user may *read* is granted by their
[profile](../configuration/user-profiles.md) (scenario → role → domain and
dimension scope), enforced inside every query. The advertised tool surface is
the [MCP tool reference](../reference/mcp-tools.md).

## What this package deliberately is not

Précis Finance MCP is the **read-only access surface** of the Précis platform. It
never writes to your data, and the agent runtime, planning and write-back,
charts, Excel export, and report building live in the full platform, not
here. The extension seams those features plug into are
documented in [Adding read tools](../development/adding-read-tools.md) — in an
open deployment they are simply unregistered.

## Where each concern lives

| To change… | Edit… | Guide |
|---|---|---|
| Metrics, statements, dimensions | `instance/catalogue/` | [Catalogue & semantic model](../configuration/catalogue-and-semantic.md), [change contract](../configuration/adding-metrics-and-dimensions.md) |
| What your data means | `instance/semantic/` (+ re-run the provisioner) | [Catalogue & semantic model](../configuration/catalogue-and-semantic.md) |
| Which datasets exist | `instance/scenarios.yml` | [Schema contract](../configuration/clickhouse-schema-contract.md) |
| How data gets in | `instance/integrations/` + credential env vars | [Ingestion](../configuration/ingestion.md) |
| Who may read what | profiles, via the admin CLI | [User profiles & permissions](../configuration/user-profiles.md) |
| How users sign in | `PRECIS_AUTH_MODE` + mode variables | [Remote access](../deployment/oauth-keycloak.md) |
| Where ClickHouse runs | `PRECIS_DATA_MODE` / `CH*` variables | [Data modes](../deployment/clickhouse-data-modes.md) |
| Any single knob | environment variables | [Reference](../configuration/environment-variables.md) |

## Vocabulary

| Term | Meaning |
|---|---|
| **Scenario** | A dataset a number comes from: actuals, a budget, a forecast. Registered in `scenarios.yml`, carried as a column in fact data. |
| **Domain** | A group of metrics sharing one source view (one catalogue file each). |
| **Metric** | A *base* metric aggregates one view column (with optional `where:` filter); a *derived* metric is a formula over other metrics. |
| **Statement** | An ordered list of metric keys rendered as a financial table (P&L, summary). |
| **Dimension** | Something you slice by. *Leaf* dimensions own a master table (`cost_centre`); *derived* dimensions are parent levels computed from a leaf's attributes (`department`). |
| **Federated domain** | A domain whose source view is read in place on your warehouse through Ibis, instead of from ClickHouse — for long-tail detail; sum-only, filters on modelled dimensions only. |
| **Source / Binding** | Ingestion config: a source is one physical origin of data; a binding ties it to one `live.*` table with an extract query and schedule. |
| **Profile** | A named permission set — scenario patterns → role → domain/dimension scope — assigned to users. |
| **Instance directory** | `instance/` — every file that describes *your* model, mounted into the deployment. |
