# What your ClickHouse must contain

If you bring your own ClickHouse (or want to understand what the bundled one
holds), this is the contract: the databases, tables, views, and the one registry
table the read engine needs before it can serve a query. The good news is you
don't hand-write most of it — you describe your model in `instance/` and the
provisioner creates the structures. This page explains what gets created and why,
so you can confirm a cluster is ready.

The engine reads **one surface only**: the `semantic.*` views. Everything else
exists to feed those views.

---

## The databases

The provisioner ensures these ClickHouse databases:

| Database | Holds | Created from |
|---|---|---|
| `live` | your actuals/master tables — the facts and dimensions | `instance/live/*.sql` |
| `staging` | a per-load twin of each `live` table (ingestion lands here, then swaps) | `instance/live/*.sql` |
| `semantic` | the views the engine queries, plus the `scenarios` registry | `instance/semantic/`, `instance/scenarios.yml` |

(A model with editable *plan* scenarios also uses a `planning` database — see
[Plan data](#plan-data) below. A read-only deployment over actuals does not.)

---

## `live.*` — your data tables

Each file in `instance/live/` becomes a table. The file carries the bare column
list + engine spec; the provisioner wraps it as `CREATE TABLE IF NOT EXISTS` in
both `live` and `staging` (the two must match exactly — that's how an atomic
refresh swaps one into the other). For example:

```sql
-- instance/live/fact_gl.sql
(
    period        String,
    account_code  String,
    cost_centre   String,
    amount        Decimal(18, 2),
    _load_id      String,
    _ingested_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, account_code, cost_centre)
```

The column names here are the start of the naming contract: your semantic views
read these columns, your catalogue references them, and clients receive them
under the same names. (`_load_id` / `_ingested_at` are audit columns the
ingestion pipeline stamps; the views drop them.)

### Changing a table that already exists

!!! warning "The provisioner creates; it does not migrate"
    `CREATE TABLE IF NOT EXISTS` means an edited DDL file is **not**
    re-applied to an existing table — re-running the provisioner silently
    leaves the old shape in place.

- **Column changes** — alter `live.<t>` *and* `staging.<t>` by hand (the two
  must stay identical; the atomic swap depends on it), or drop both and
  re-run the provisioner.
- **Binding kind changes** (`period` ↔ `snapshot`) — the DDL shape itself
  changes (`PARTITION BY` appears or disappears), which `IF NOT EXISTS`
  won't re-emit: `DROP TABLE live.<t>` and `DROP TABLE staging.<t>`, then
  re-run the provisioner.

(Views are unaffected — `CREATE OR REPLACE VIEW` always re-applies.)

---

## `semantic.*` — the views the engine reads

Each file in `instance/semantic/dims/` and `instance/semantic/views/` becomes a
view (`CREATE OR REPLACE VIEW semantic.<name>`). Dimensions are created first
because the fact views reference them. These views are *your* business logic —
what a P&L line is, how a period rolls up — over the `live.*` tables. See
[Catalogue & semantic model](catalogue-and-semantic.md) for how to write them.

The contract the engine relies on: every `source_view` your catalogue names must
exist here as a real view. The provisioner creates them; `--check` confirms it.

---

## `semantic.scenarios` — the scenario registry

A **scenario** identifies which dataset a number comes from — actuals, a budget, a
forecast. The engine loads the list of valid scenarios from one table,
`semantic.scenarios`, at startup. Unlike the views, this table's *shape* is fixed
by the platform; you supply its *rows* in `instance/scenarios.yml`:

```yaml
# instance/scenarios.yml
scenarios:
  - scenario_id: ACTUALS
    alias: actuals
    name: Actuals
    kind: ACTUAL
  - scenario_id: BUD-2026
    alias: budget
    name: Budget 2026
    kind: BUDGET
    base_scenario: ACTUALS
```

Required per scenario: `scenario_id`, `alias` (the short key clients use), `name`,
`kind`. Everything else (status, horizon, description, …) is optional with sane
defaults. At minimum, declare the scenario your actuals live under.

The provisioner reads this file and **seeds only the scenarios that aren't already
there** — so re-running never overwrites a scenario whose state has since changed.

---

## Plan data

If your model includes **editable plan scenarios** (a budget or forecast users
revise in Précis, not just read), those rows live in a `planning` database that
the read engine's semantic views union in. Writing and provisioning plan data
belongs to the **Précis platform**, beyond the read-only open package — the
open provisioner (`--scope open`) does not create the `planning` tables. A read-only
deployment over actuals needs none of this; your views read `live.*` and that's
it.

---

## Confirming a cluster is ready

After provisioning (or against a cluster you populated yourself), run the
preflight:

```bash
python -m precis_mcp.clickhouse_init --scope open --check
```

It validates, without changing anything, that:

- your catalogue parses and is internally consistent;
- every `semantic.*` view your catalogue names **exists** in ClickHouse;
- `semantic.scenarios` exists and has at least one row.

It prints a line per check and exits non-zero on any failure — so a missing view
or an empty registry is a clear message before go-live, not a confusing error
when a client first queries.

---

## Related

- [ClickHouse data modes](../deployment/clickhouse-data-modes.md) — bundled vs.
  your own, and the provisioner that creates all of the above.
- [Catalogue & semantic model](catalogue-and-semantic.md) — writing the `live`
  DDL and `semantic` views.
- [Ingestion & data sources](ingestion.md) — loading rows into the `live` tables.
