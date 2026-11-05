# MCP tool reference

The tools an MCP client sees on the authenticated `/mcp` endpoint, with their
parameters and behaviours. The single-user dev server
([quickstart](../getting-started/quickstart.md)) serves the same underlying
tools over Streamable HTTP. Financial access is read-only. Artifact tools can create owned temporary files and upload reservations; they never ingest or mutate financial data.

## Conventions that apply to every tool

- **Auth and scope.** Every call carries the caller's bearer token. Tools
  that take a scenario reference (`scenarios`, `scenario_id`) are checked
  against the caller's [profile](../configuration/user-profiles.md) before
  execution, and the profile's domain/dimension scope is applied *inside*
  the query — out-of-scope rows simply don't appear.
- **Errors are data.** Tools don't raise; failures return
  `{"error": "...", "error_type": "..."}` so the calling model can read the
  message and correct itself.
- **Concurrency is bounded.** A burst of read calls — e.g. a spreadsheet
  recalculating many cells at once — is capped per user (and globally) so it
  can't overload the read layer. Over the cap, a call returns a retryable
  "busy" tool error rather than queueing indefinitely; retry shortly. A
  well-behaved client keeps its own in-flight requests below the cap and never
  sees it. See the [concurrency caps](../configuration/environment-variables.md#read-concurrency-the-mcp-read-path).
- **Results arrive on both channels.** The JSON result is returned as
  `structuredContent` and as JSON text in `content` — a client without
  widget support loses nothing but the rendering.
- **Names are catalogue keys.** Metric keys, statement names, dimension
  keys, and scenario aliases all come from *your*
  [catalogue](../configuration/catalogue-and-semantic.md) and scenario
  registry. The discovery tools below return the valid values; the listings
  here use the bundled demo model for examples.
- **Scenario keys are not headings.** In every reporting call, give each
  scenario object a concise user-facing `alias` (`Actuals`, `Budget`,
  `Variance`, `Var %`, `Prior Year`). If omitted, the internal scenario key is
  used as the visible column heading.

## Variants and widgets

A widget is bound to a tool *definition*, so the two reporting tools are
advertised twice — once rendering, once raw:

| Advertised name | Returns |
|---|---|
| `run_statement` / `run_metric` | the raw figures (same as the `_data` variant), plus a formatted table carried as a widget render block; linked to the **financial-table** widget |
| `run_statement_data` / `run_metric_data` | the raw figures, for the model to reason over |
| `inspect_rows` | row detail; linked to the **inspection-grid** widget |
| everything else | plain data |

Both the rendering and `_data` variants return the **same** raw figures to the
model; the rendering variant only *additionally* carries the formatted table as
a widget render block on the result's `_meta` (hidden from the model). Widgets
follow the MCP Apps extension (`_meta.ui.resourceUri` on the tool, bundle
fetched via `resources/read`); a widget is only advertised when its bundle is
built. Hosts that don't render widgets still get the full result as JSON. Three parameters are stripped from every advertised schema (`out`,
`report_id`, `position` — output paths that don't exist on this transport).
Report persistence and workbook-editing parameters are absent from this remote surface. Use `export_dataset` for temporary JSON/XLSX downloads.

Financial widgets follow the host's light/dark theme and supplied fonts and
colours. Statement labels use sans-serif text and figures use monospace with
financial formatting. Chart series retain the Précis palette; tooltips and
heatmaps use solid, theme-aware fallbacks when the host supplies only a theme
signal. Heatmaps keep primary text over a restrained teal tint. These
presentation choices do not alter the raw figures returned by tools.

The reporting render-variant descriptions instruct the agent to let the widget
display the table and add useful interpretation, without reproducing the table
or restating all rows in Markdown. Reproduction is allowed when explicitly
requested or the widget is unavailable. The `_data` variants show no widget and
keep their separate raw-data guidance.

## Orientation

### `precis_orientation` — call first

No parameters. Returns the orientation text for this deployment: the data
model (scenarios, metrics, statements, dimensions), how the tool variants
relate, and usage guidance. For catalogues that expose `full_pnl`, this guidance
prefers it for a general P&L request when the user has not chosen another
statement and no report default applies. It exists because hosted clients drop
or truncate the MCP `initialize` instructions — a tool *result* is the one
channel that reliably reaches the model. A well-behaved client calls it once
before composing queries.

## Discovery

### `list_scenarios`

No parameters. The scenarios the caller may read — id, alias, name, kind,
status — plus the generated comparison vocabulary (variance and shifted
views). Scenarios the caller's profile doesn't grant are omitted entirely.

### `list_kpis`

No parameters. The metric catalogue: every metric's key, label, format,
domain, and description, plus per domain the `dimension_keys` (one list of
catalogue dimension names, each valid in both `filters` and `dimensions`) and
`axis_only_dimensions` (inline federated axes valid only in `dimensions`). This
is the map for composing `run_metric` calls.

### `search_hierarchy`

| Parameter | Type | Notes |
|---|---|---|
| `query` | string, optional | Free-text match on names/codes (`"cloud"`, `"Smith"`). Omit to list a whole dimension. |
| `dimension` | string, optional | Restrict to one dimension key (`cost_centre`, `employee`, …). Recommended when listing. |
| `limit` | int, optional | Per-section result cap. Defaults to 200 records + 100 nodes when listing (50 + 20 when searching); capped at 10000. Raise it to list a large dimension completely. |

Finds valid member ids before filtering. Covers all three dimension kinds —
leaf, derived, and ragged. Returns `records` (leaf and derived members,
with the exact filter key to use) and `hierarchy_nodes` (rollup nodes —
filter with the node id exactly as returned). Visibility is scoped: a member
is returned only if at least one readable scenario allows it.

Unknown dimension **keys** are catalogue errors: use `precis_orientation` or
`list_kpis` to discover the supported names. Unknown member **values** direct
the caller to `search_hierarchy`; validation errors do not list member
alternatives. Discovery applies the caller's visibility scope.

### `list_dimensions`

No parameters. The dimension catalogue as metadata — each entry carries `key`
(valid in `filters` and `dimensions`), `label`, `kind` (`leaf`, `derived`, or
`ragged`), for ragged hierarchies the `leaf_dimension` they aggregate, and for
time dimensions a `grain` (`date`, `week`, `month`, `quarter`, `year`) — the
period grain that dimension carries.
It never returns members — that is `search_hierarchy`'s job; use this tool
only to discover which dimension keys exist.

### `list_variants`

| Parameter | Type | Notes |
|---|---|---|
| `scenario_id` | string, required | Scenario-gated. |

What-if variants of a scenario, where the model defines them.

## Reporting

### `run_statement` / `run_statement_data`

A financial statement — rows are the statement's lines, columns are
scenarios, optionally crossed with a dimension breakdown.

| Parameter | Type | Notes |
|---|---|---|
| `statement` | string | Statement key from the catalogue. The calling model is guided to prefer `full_pnl` for a general P&L when available; the transport fallback remains `pnl` for catalogue portability. |
| `scenarios` | list of objects | Each `{"scenario": "<key>", "alias": "<display label>"}` — registry keys, variance keys, shifted views. Always provide the user-facing `alias`; the field remains optional at protocol level for compatibility. Scenario-gated. |
| `period_start` / `period_end` | string | Grain-tagged period range; the code shape sets the grain — `2025-06` month, `2025-Q2` quarter, `2025` fiscal year, `2025-W37` week, `2025-06-14` day. Both bounds must be the same grain. Month/quarter/fiscal-year work everywhere; week/day only where the domain's data carries them. |
| `filters` | object | Dimension key → member id(s). Keys from `dimension_keys`; member ids from `search_hierarchy`. |
| `dimensions` | list of strings | Breakdown dimension keys — the same `dimension_keys` (e.g. `["period"]`, `["cost_centre"]`). |
| `scale` | int | Currency scaling power: `0` units, `3` thousands, `6` millions. |
| `decimals` | int | Decimal places. |

A statement may span several domains; `period` and `cost_centre` break down
every line, other dimensions break down only the lines from compatible
domains (the rest show aggregates).

### `run_metric` / `run_metric_data`

One or more metrics broken down by dimensions — anything that isn't a
standard statement: revenue by project, utilisation by employee, a GL
drill-down.

| Parameter | Type | Notes |
|---|---|---|
| `metrics` | list of strings, required | Metric keys — all from the same domain (`list_kpis` shows each metric's domain). |
| *(rest)* | | Identical to `run_statement`: `scenarios`, `period_start`/`period_end`, `filters`, `dimensions`, `scale`, `decimals`. |

Rows are the dimension members; columns are metrics × scenarios. Breakdown
dimensions must be available in the metrics' domain.

## Row-level inspection

### `list_inspection_sources`

No parameters. The row-level sources the catalogue exposes for
drill-through (domains with `inspect_enabled: true`).

### `get_inspection_schema`

| Parameter | Type | Notes |
|---|---|---|
| `source_key` | string, required | From `list_inspection_sources`. |

The columns and filterable dimensions of one inspection source — call it
before composing an `inspect_rows` query.

### `inspect_rows`

The drill-through from a figure to the rows behind it.

| Parameter | Type | Notes |
|---|---|---|
| `source_key` | string, required | From `list_inspection_sources`. |
| `scenario_id` | string, required | Scenario-gated — required so the permission check runs before any rows are read. |
| `filters` | object | Semantic dimension keys from the source's schema. |
| `columns` | list of strings | Restrict output columns (must be within the source's configured `inspect_columns`). |
| `limit` | int | Row cap; the server enforces a hard ceiling (`INSPECTION_ROW_CAP`, default 10 000). |
| `period_start` / `period_end` | string | Grain-tagged period range; the code shape sets the grain — `2025-06` month, `2025-Q2` quarter, `2025` fiscal year, `2025-W37` week, `2025-06-14` day. Both bounds must be the same grain. Month/quarter/fiscal-year work everywhere; week/day only where the domain's data carries them. |

Returns a capped sample of rows plus the inspection grid for rendering
hosts. The external inspection widget displays at most its first 50 preview
rows and does not provide server-side pagination; refine filters or use an
`export_dataset` with the returned `data_ref` for the complete bounded result. The shared Redis/Valkey cache supplies that snapshot before preview truncation; the open server provides no authenticated SPA paging route. Profile
scope applies to the rows themselves. In the result metadata,
`truncated=true` means the database inspection hit its hard/query limit;
`agent_truncated=true` means only the returned reasoning/preview payload was
shortened.

## Ingestion status

Available only in Précis-managed ingestion mode. With
`PRECIS_INGESTION_MODE=customer_managed`, the ingestion tool family—including
`reload_integrations`—is omitted on either primary backend. Customer pipelines
own freshness reporting in that mode; see [Analytical client](../configuration/analytical-client.md).

Data-freshness questions, answerable by any user from the client — "is April
in yet?", "when was this last loaded?". These read the
[ingestion](../configuration/ingestion.md) configuration and audit trail;
they carry no financial figures and no scenario parameter.

### `list_load_history`

| Parameter | Type | Notes |
|---|---|---|
| `binding_id` | string, optional | Restrict to one data feed. |
| `dataset_id` | string, optional | Restrict to one dataset across sources. |
| `period` | string, optional | One accounting period. |
| `status` | string, optional | `running`, `success`, or a `failed_*` bucket. |
| `limit` | int | Default 50, capped at 200. Most recent first. |

Load attempts from the `load_history` audit trail; filters AND together.

### `get_load_status`

| Parameter | Type | Notes |
|---|---|---|
| `load_id` | string, required | From `list_load_history`. |

One load's full detail — timestamps, status, rows landed, error message.

### `list_bindings`

| Parameter | Type | Notes |
|---|---|---|
| `source_id` | string, optional | Restrict to one source. |
| `target` | string, optional | Restrict to one `live.*` target. |

The configured data feeds with their schedules.

### `get_binding`

| Parameter | Type | Notes |
|---|---|---|
| `binding_id` | string, required | From `list_bindings`. |

One feed's full configuration: source, target, schedule, extract parameters
(operator metadata — the extract query is visible here, credentials never
are; they live in environment variables, not in the configuration).

## A typical first session

```text
precis_orientation                  → how this deployment's model works
list_scenarios                      → "actuals" and "budget" exist
list_kpis                           → revenue lives in domain pnl; can break by cost_centre
search_hierarchy(dimension="cost_centre", query="cloud")
                                    → the member ids worth filtering on
run_metric_data(metrics=["revenue"], scenarios=[{"scenario": "actuals", "alias": "Actuals"}],
                dimensions=["cost_centre"], period_start="2026-01",
                period_end="2026-03")
                                    → the figures
inspect_rows(source_key="gl", scenario_id="actuals",
             filters={"cost_centre": "CC-CLOUD-01"})
                                    → the rows behind the number
```

## Related

- [Adding read tools](../development/adding-read-tools.md) — the developer
  contract for extending this surface.
- [User profiles & permissions](../configuration/user-profiles.md) — the
  scoping every call above passes through.
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md) —
  where the keys these tools accept are defined.

## Charts and artifacts

Chart transforms receive full-precision cached values **already scaled** by
the engine. `scale: 0` means units and `scale: 3` divides non-exempt values by
1000. Percentages (`75` means 75%) and scale-exempt metrics such as FTEs retain
their units even in mixed statements. Do not scale the values again; chart
`y_scale` is forced to `0`, and left-axis decimals come from the first input.
For multiple sources, inspect every scale and metric format. Re-query
same-unit series at a common scale before sharing an axis. Use separately
labelled left/right axes for different units on line/bar/area charts, or use
separate charts; set right-axis format and decimals explicitly when needed.
The tool does not reconcile units automatically. Merged captions omit
conflicting nonempty scale labels; an unlabelled source does not remove another
source's label. Axis labels must therefore state each series' units.

| Tool | Parameters | Behaviour |
|---|---|---|
| `eval_chart_transform` | `data_refs: list[str]`, `transform_code: str`, `chart_spec: object` | Transform cached snapshots with bounded QuickJS; supports line, bar, area, waterfall, scatter, horizontal_bar, forecast_line, cohort_heatmap and sparkline_row. Always pass an array, even for one input. |
| `export_dataset` | `data_ref: str`, `format: json/xlsx` | Export the cached result without rerunning the query; returns an owned artifact and HTTPS download link. |
| `get_file_download` | `file_id: str` | Renew a download URL while the file remains available and authorized. |
| `begin_file_upload` | `filename: str`, `mime_type: str`, `size_bytes: int` | Reserve quota and return a short-lived PUT URL. |
| `complete_file_upload` | `upload_id: str` | Register received raw bytes once; retry returns the same identity. |
| `import_chatgpt_file` (optional) | `file: object` | Import a host attachment through the explicitly configured HTTPS hostname allow-list. |

Upload reservation/completion and attachment import are disabled by default.
Set `PRECIS_MCP_UPLOADS_ENABLED=true` to enable them; the ChatGPT importer also
requires `PRECIS_FILE_IMPORT_HOSTS`. Disabled tools are hidden and direct calls
are rejected. `export_dataset` and `get_file_download` remain enabled.

Read tools return opaque `ds_` references when snapshot caching succeeds. Idle/absolute expiry, owner isolation and current-grant revalidation apply. Cache unavailability leaves the read usable with `dataset_available=false`; rerun after recovery to obtain a reference. No query-specific `_excel` tools are advertised. See [Files and external agent hosts](files-and-hosts.md) for transfer lifecycle, host limitations and operator requirements.
