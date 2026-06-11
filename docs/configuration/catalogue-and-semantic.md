# Catalogue & semantic model

This page walks one real example end to end — a small professional-services P&L —
so you can see how every layer connects. By the end you'll be able to read any
metric back to the SQL it runs and the field a client receives.

## The two layers

You describe your model in two layers, kept separate on purpose:

- **Semantic layer** — SQL views that say *what your data means*: what a P&L row
  is, which accounts are revenue, how a period rolls up. This is where business
  logic lives.
- **Catalogue** — YAML that says *what gets exposed*: which metrics, dimensions,
  and statements exist, and how each is computed and formatted. This is the
  surface clients see.

The catalogue sits on top of the semantic layer and refers to it by column name.
**The names must line up** — and the engine checks this at startup, so a mismatch
is an error you see immediately, not a wrong number you discover later.

```text
instance/
  catalogue/        # YAML — what gets exposed
    pnl.yml             a domain: its source view + metrics
    dimensions.yml      the dimension registry (how you slice)
    statements.yml      named collections of metrics
  semantic/         # SQL — what the data means
    dims/               dimension master-data views
    views/              fact/metric views the engine queries
```

This directory is *your* configuration — it describes your model and ships with
your deployment, separate from the installed package.

The example below uses a services-business model (revenue, delivery costs,
margins, headcount). Substitute your own accounts and metrics; the mechanics are
the same.

---

## Layer 1 — the semantic views

### A dimension view

A dimension is a thing you slice by — account, cost centre, period. Each owns a
master-data view under `semantic/dims/`. Here is the whole account dimension:

```sql
-- semantic/dims/dim_account.sql
-- The chart of accounts, from the ERP master. Excludes non-postable header rows.
SELECT
    account_code,
    account_name,
    account_type,
    fs_line          -- which financial-statement line this account belongs to
FROM live.dim_account
WHERE is_active = TRUE
  AND account_type != 'HEADER'
ORDER BY account_code
```

The columns it exposes — `account_code`, `account_name`, `account_type`,
`fs_line` — are the names the catalogue will refer to. Remember `fs_line`: the
revenue metric uses it.

### A fact view

A fact view is what the engine actually queries for numbers. It produces one tidy
table at a known grain — one row per *(account, cost centre, period, scenario)* —
with a measure column. Here is the P&L view, **abridged** to its shape (the full
view also unions plan/forecast scenarios and statistical sections like hours and
FTEs):

```sql
-- semantic/views/v_pnl.sql  (abridged)
WITH unified AS (
    -- Actuals, from the posted general ledger
    SELECT
        account_code AS account,
        cost_centre  AS cost_centre,
        period       AS period,
        'ACTUALS'    AS scenario,
        SUM(amount)  AS amount
    FROM live.fact_gl
    GROUP BY account_code, cost_centre, period

    -- … UNION ALL the budget/forecast scenarios, plus statistical
    --   sections (hours, FTEs) — omitted here …
)
SELECT
    u.account,
    ad.fs_line,                 -- pulled in from the account dimension
    u.cost_centre,
    u.period,
    u.scenario,                 -- which dataset this number is from
    u.amount                    -- the measure
FROM unified u
LEFT JOIN live.dim_account ad ON u.account = ad.account_code
```

The columns this view exposes are the contract the catalogue builds on:

| Column | Role | Used by |
|---|---|---|
| `account`, `cost_centre`, `period` | dimension keys — what you group by | metric dimensions |
| `fs_line` | an account attribute — what you filter on | the `revenue` metric's filter |
| `scenario` | which dataset (actuals, a budget, a forecast) | scenario selection at query time |
| `amount` | the measure the engine sums | every base metric's `source_column` |

---

## Layer 2 — the catalogue

### Binding a domain to its view

A **domain** is a group of metrics that share one source view — the P&L
metrics over the P&L view, the pipeline metrics over the pipeline view. Each
domain is one catalogue file, and the file names the semantic view it sits on.
This one line is the join between the two layers:

```yaml
# catalogue/pnl.yml
domain: pnl
source_view: semantic.v_pnl     # ← every metric below queries this view

dimensions:                     # which columns of the view you may slice by
  - { key: cost_centre, label: Cost Centre, source: cost_centre }
  - { key: period,      label: Period,       source: period }
```

`source:` is the view column; `key:` is the name clients use. They match here, and
they must resolve to a real column in `v_pnl`.

### A base metric

A base metric reads the measure column directly, optionally filtered, then
aggregates and formats. Here is `revenue`:

```yaml
  - key: revenue
    label: Revenue
    where:                         # restrict to revenue accounts…
      - column: fs_line
        op: eq
        value: Revenue
    source_column: amount          # …sum this column…
    aggregation: sum               # the SQL aggregate over source rows
    rollup_method: sum             # how aggregated values combine across periods
    sign: abs                      # ledger stores revenue negative; flip to positive
    format: currency
    fs_group: Revenue              # which statement section the metric belongs to
```

Two of these look similar but answer different questions: `aggregation` is the
SQL aggregate applied to the source rows (`sum`, `count`, `avg`, …);
`rollup_method` is how the already-aggregated values combine when periods roll
up — `sum` for flows like revenue, `closing` for balances (take the last
period's value rather than adding), `avg` for rates.

Read it as a query against the source view:

```sql
SELECT SUM(amount)
FROM   semantic.v_pnl
WHERE  fs_line = 'Revenue'
  AND  scenario = :scenario       -- chosen at query time
GROUP BY :requested_dimensions    -- e.g. cost_centre, period
```

Every field traces somewhere: `where` and `source_column` reference columns in
`v_pnl`; `sign` and `format` shape the output; `key` becomes the field name the
client receives.

#### The `where` predicate

`where` is a **portable filter** — a list of structured predicates, ANDed
together. It replaces raw SQL filter strings so the same metric definition works
against a native ClickHouse view *or* a **federated** source — a table the
engine reads in place on your warehouse through Ibis, instead of from
ClickHouse (see
[Adding metrics & dimensions](adding-metrics-and-dimensions.md)).
The engine compiles the predicates to whichever backend the source view uses.

```yaml
    where:
      - column: account_type
        op: in
        values: [Revenue, OtherIncome]   # `in`/`not_in` take `values:` (a list)
      - column: is_intercompany
        op: eq
        value: false                     # other ops take a single `value:`
```

Supported `op`s: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`,
`is_null`, `is_not_null`. The last two take neither `value` nor `values`.
`where` is the only filter grammar — the loader rejects a catalogue that uses
the retired `source_filter` string.

### A derived metric

A derived metric has no `source_column` — it's a `formula` over other metric
keys. Each input is aggregated independently first, then combined:

```yaml
  - key: gross_margin
    label: Gross Margin
    formula: "revenue - direct_cost"       # references two other metric keys
    format: currency
    fs_group: Margins

  - key: gross_margin_pct
    label: "Gross Margin %"
    formula: "gross_margin / revenue * 100"  # derived metrics can build on derived metrics
    format: percent
    fs_group: Margins
```

`revenue` and `direct_cost` here are the *exact keys* of other metrics in the
catalogue. A typo is a load-time error, not a silent zero.

### A statement

A statement is an ordered list of metric keys — what the engine assembles into a
financial table:

```yaml
# catalogue/statements.yml
statements:
  pnl:
    label: "P&L Statement"
    lines:
      - revenue
      - direct_cost
      - gross_margin
      - gross_margin_pct
      - separator              # a visual rule, not a metric
      - indirect_cost
      - contribution_margin
      - sga
      - ebitda
      - ebitda_margin_pct
```

Each line is a metric key from the catalogue. Asking for the `pnl` statement runs
each metric and stacks the results in this order.

---

## How you slice — the dimension registry

`catalogue/dimensions.yml` defines each dimension once: its master-data view, its
key column, its display attribute, and its place in any hierarchy. The `account`
dimension, mapping onto the SQL view from earlier:

```yaml
# catalogue/dimensions.yml
account:
  label: Account
  display_attribute: name
  source:
    table: semantic.dim_account     # ← the dimension view from Layer 1
    key_column: account_code
    attribute_mapping:
      name: account_name
  parents:                          # the hierarchy this dimension rolls up into
    fs_line:      { source_column: fs_line }
    account_type: { source_column: account_type }
```

`parents` declares hierarchy bottom-up: every account belongs to an `fs_line` and
an `account_type`. Those become **derived dimensions** — dimensions whose members
are attribute values of another. This is how the `revenue` metric can filter on
`fs_line = 'Revenue'` even though `fs_line` isn't its own table: it's an attribute
of `account`.

A domain's `dimensions:` block (in `pnl.yml`) is the subset of these you can slice
*that view* by. The registry defines all dimensions; each domain opts into the
ones its view supports.

---

## Scenarios

A **scenario** identifies which dataset a number comes from — actuals, a budget, a
forecast. It's the `scenario` column in the fact view, and clients choose one (or
compare two) at query time. You don't define scenarios in a catalogue file; the
engine exposes whatever scenario values exist in your data.

---

## Tracing one query

Putting it together — *"revenue by cost centre for the P&L, actuals":*

1. The client asks for metric `revenue`, grouped by `cost_centre`.
2. The engine looks up `revenue` in `pnl.yml`, sees `source_view: semantic.v_pnl`.
3. It runs `SELECT SUM(amount) … WHERE fs_line = 'Revenue' AND scenario = 'ACTUALS' GROUP BY cost_centre`.
4. It applies `sign: abs` and `format: currency`.
5. It returns rows keyed by `cost_centre`, with the metric under the field name
   `revenue`.

Every step is something you declared. Nothing is implicit.

---

## The naming contract

The one rule that ties it all together: **the same name appears in every layer.**

```
semantic view column   →   catalogue reference        →   client field
   amount                    source_column: amount
   fs_line                   where: [{column: fs_line}]
   account_code              key_column: account_code
   revenue (metric key)      lines: [revenue, …]            revenue
```

If a name doesn't line up — a metric points at a column the view doesn't have, a
statement lists a metric key that doesn't exist, a dimension maps to a missing
column — the engine reports it at startup and refuses to serve an inconsistent
model. Fix the name; restart; it's caught before any client sees a number.

You don't have to wait for a restart to find out, though. Run the model check
ahead of time — it validates the catalogue *and* confirms the semantic views it
names exist in ClickHouse, without starting the server or changing anything:

```bash
python -m precis_mcp.clickhouse_init --scope open --check
```

See [What your ClickHouse must contain](clickhouse-schema-contract.md) for the
full preflight.

## Related

- [Ingestion & data sources](ingestion.md) — getting data into the views above.
- [Quickstart](../getting-started/quickstart.md)
