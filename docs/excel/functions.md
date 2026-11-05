---
description: Reference the PRECIS Excel custom functions for governed financial statements, metrics, scenarios, hierarchies, and drill-down.
---

# Excel function reference

All functions live in the **`PRECIS`** namespace and **spill** their result into
the grid. Style a spilled table with the **Format** ribbon button; re-fetch every
cell with **Refresh**.

!!! note "Argument separator"
    Examples below use `;`. Excel uses your locale's list separator — `,` in some
    locales, `;` in others. Skip an optional middle argument with two separators,
    e.g. `…;;…`. Trailing optional arguments can simply be omitted.

Discover valid keys with [`=PRECIS.KPIS()`](#preciskpis) (metric keys),
[`=PRECIS.SCENARIOS()`](#precisscenarios) (scenario keys), and
[`=PRECIS.HIERARCHY(…)`](#precishierarchy) (dimension members).

---

## PRECIS.STATEMENT

A financial statement — rows are statement lines (Revenue → EBITDA), columns are
scenarios.

```
=PRECIS.STATEMENT(statement, periodStart, periodEnd, [scenarios], [dimensions], [filters], [scale], [decimals], [layout])
```

| Argument | Required | Description |
|---|---|---|
| `statement` | yes | Statement key, e.g. `"pnl"`, `"full_pnl"`. |
| `periodStart` | yes | Range start, `YYYY-MM`, e.g. `"2026-01"`. |
| `periodEnd` | yes | Range end, `YYYY-MM`. |
| `scenarios` | no | Comma-separated scenario keys. Optional display alias via `key as Alias`. |
| `dimensions` | no | Comma-separated dimensions to break by, e.g. `"cost_centre"`. |
| `filters` | no | `key=value` pairs, comma-separated; multi-value with `\|`. |
| `scale` | no | Currency scaling power: `0`=units, `3`=thousands, `6`=millions. |
| `decimals` | no | Displayed decimal places; defaults to `1` when omitted. |
| `layout` | no | `report` (default) or `extract` (flat detail rows with stable line-item keys). |

```excel
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals as Act,budget as Bud,actuals_vs_budget as Var")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py as YoY")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals";"cost_centre")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals";;"cost_centre=CC-100|CC-200")
=PRECIS.STATEMENT("full_pnl";"2026-01";"2026-04";"actuals";;;6;1)
```

With a dimension breakdown **and** more than one scenario, the statement gains a
two-row header — each dimension group sits above its scenario columns — and the
total column is shaded so it stands out. Click **Format** to apply the full
Précis styling.

---

## PRECIS.METRIC

A metric breakdown — one or more metrics, broken down by dimensions (rows) and
scenarios (columns).

```
=PRECIS.METRIC(metrics, periodStart, periodEnd, [dimensions], [scenarios], [filters], [layout], [scale], [decimals])
```

| Argument | Required | Description |
|---|---|---|
| `metrics` | yes | Comma-separated metric keys, e.g. `"revenue,utilisation"`. |
| `periodStart` | yes | Range start, `YYYY-MM`. |
| `periodEnd` | yes | Range end, `YYYY-MM`. |
| `dimensions` | no | Comma-separated dimensions to break by. |
| `scenarios` | no | Comma-separated scenario keys; alias via `key as Alias`. |
| `filters` | no | `key=value` pairs, comma-separated; multi-value with `\|`. |
| `layout` | no | `report` (default — styled, hierarchical) or `extract` (flat, formula-friendly leaf table). |
| `scale` | no | `0`/`3`/`6`. |
| `decimals` | no | Displayed decimal places; defaults to `1` when omitted. |

With more than one metric **and** more than one scenario, the report layout adds
a two-row header: the metric label sits above its scenario columns (a spill can't
merge cells, so the label appears at the start of each group). The `extract`
layout returns a flat leaf table with the dimensions as columns — use it for
downstream formulas. **Format** applies its latest per-metric number formats.
After a recalculation changes the spill's shape or row order, use **Format**
again to align those formats with the current rows. Values retain full available
engine/Excel precision; decimal places control display. Multi-metric extracts
use metric keys rather than display labels.
Both layouts use server worksheet projections and retain engine percentage
units (8.7 means 8.7%). Report exports and the add-in resolve formats per cell:
percentage-comparison columns take precedence, otherwise the metric's unit and
decimal settings apply. Both financial layouts end with the same single context
footer as native static exports. It contains scenarios, period, filters,
dimensions, units and timing information in platform caption-bar order. Text is
in the first cell of the final row, with other cells blank. Conversion and Apply
Précis formatting leave that cell unwrapped. Conversion preserves shared rows; the
Format action autofits the whole footer row. Typed formulas use the existing
explicit-formatting workflow. Report headers are unmerged. A standalone export's
title is outside the managed region and remains user maintained. Extract Tables
and generated value-column names exclude the footer; dynamic names end at
`ROWS(spill)-1`. Decorative separators are omitted while semantic headings,
subtotals and totals remain. Fresh results can change body-row positions.
File adapters use the same statement/metric argument contract, including explicit
zero scale/decimals and the normal omitted-argument defaults. They do not evaluate arbitrary
Excel expressions: headless conversion requires saved spill bounds and literal
or non-formula scalar reference arguments. Keep live execution in Excel; a
structurally generated file is not evidence that the add-in calculated it.

```excel
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"cost_centre")
=PRECIS.METRIC("revenue,utilisation";"2026-01";"2026-05";"department,cost_centre";"actuals as Act,budget as Bud")
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"project";"actuals";;"extract")
```

---

## Comparison and prior-year scenarios

Any scenario column in [`STATEMENT`](#precisstatement) or
[`METRIC`](#precismetric) can be a **generated** scenario, not just a real one:

| Key pattern | Example | Gives you |
|---|---|---|
| `<alias>_py` / `<alias>_pp` | `actuals_py` | the scenario shifted a year (`_py`, −12 months) or a period (`_pp`, −1 month) back |
| `<left>_vs_<right>` | `actuals_vs_budget` | signed variance, `left − right` |
| `<left>_vs_<right>_pct` | `actuals_vs_actuals_py_pct` | percentage variance |

Year-on-year is just a comparison against a period-shifted scenario —
`actuals_vs_actuals_py` (signed) or `actuals_vs_actuals_py_pct` (percent).
`prior_year` / `prior_period` work as aliases for `actuals_py` / `actuals_pp`.

```excel
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py as YoY")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py_pct as YoY%")
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"cost_centre";"actuals,budget,actuals_vs_budget as Var")
```

Comparison columns are **colour-coded** favourable/unfavourable when you click
**Format**: green/red follows each metric's variance polarity (revenue up is
green, cost up is red), so a cost variance is never mis-coloured. Discover the
live keys with `=PRECIS.SCENARIOS("shifted")` and `=PRECIS.SCENARIOS("comparisons")`.

---

## PRECIS.HIERARCHY

List or search dimension members and ragged-hierarchy nodes.

```
=PRECIS.HIERARCHY(dimension, [query], [output])
```

| Argument | Required | Description |
|---|---|---|
| `dimension` | yes (to list) | Dimension key, e.g. `"cost_centre"`, `"employee"`. |
| `query` | no | Free-text search, e.g. `"cloud"`; omit to list all members. |
| `output` | no | `records` (leaf members, default), `nodes` (ragged rollup nodes), or `list` (two columns — member code and display name — with no header row). |

```excel
=PRECIS.HIERARCHY("cost_centre")
=PRECIS.HIERARCHY("cost_centre";;"nodes")
=PRECIS.HIERARCHY("employee";"smith")
=PRECIS.HIERARCHY("department";;"list")
```

`list` works for every dimension kind — leaf, derived, and ragged (for a
hierarchy key it lists every node, all levels) — and lifts the server's
default result cap so the list is complete. It is the shape behind the task
pane's [member drop-downs](index.md#member-drop-downs); use it directly
whenever a formula needs a clean code + name lookup table.

---

## PRECIS.KPIS

The metric catalogue as a flat table — keys, labels, domains, formats, and the
dimensions available for each. Use it to find valid metric keys for
[`=PRECIS.METRIC`](#precismetric).

```
=PRECIS.KPIS()
```

```excel
=PRECIS.KPIS()
```

---

## PRECIS.SCENARIOS

The available scenarios as a flat table — the keys/aliases to pass to
[`=PRECIS.STATEMENT`](#precisstatement) and [`=PRECIS.METRIC`](#precismetric).
Scope-filtered to what you can read.

```
=PRECIS.SCENARIOS([section])
```

| Argument | Required | Description |
|---|---|---|
| `section` | no | `real` (concrete scenarios, default), `comparisons` (generated variance keys such as `actuals_vs_budget`), `shifted` (period-shifted keys such as `actuals_py`), or `aliases` (compatibility aliases, e.g. `prior_year` → `actuals_py`). |

See [Comparison and prior-year scenarios](#comparison-and-prior-year-scenarios) for what
the `comparisons` and `shifted` keys mean and how to use them as columns.

```excel
=PRECIS.SCENARIOS()
=PRECIS.SCENARIOS("comparisons")
=PRECIS.SCENARIOS("shifted")
```
