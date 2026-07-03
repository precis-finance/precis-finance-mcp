# Adding read tools

Précis-MCP exposes your financial model to MCP clients through a small set of
read-only tools — `run_statement`, `run_metric`, `inspect_rows`, the
discovery tools, and the ingestion-status reads (the advertised set is
catalogued in the [MCP tool reference](../reference/mcp-tools.md)). This
guide is the contract for adding a tool of your own: where the function
lives, how it gets registered and advertised, what the permission gate does
for you, and what your tool must still do itself.

Everything here stays inside the `precis_mcp` package. Précis-MCP is read-only
by design — a new tool may query ClickHouse, the catalogue, or the platform
database, but it must not write to or mutate any of them.

## Architecture sketch of this slice

A tool call over the `/mcp` endpoint passes through four stations:

```text
tools/list                          tools/call
    │                                   │
[1] TOOL_CATALOGUE + build_descriptors  │   precis_mcp/dispatch.py
    │   (what exists, with what access) │
[2] mcp_tool_variants + descriptions    │   precis_mcp/mcp_external/framing.py
    │   (how it is advertised)          │
    └──────────────►  [3] process_tool_call (coercion, context, permission gate)
                          │                 precis_mcp/dispatch.py
                      [4] your tool function, then frame_tool_result
                          │                 precis_mcp/tools/*, framing.py
                          ▼
                      JSON-RPC result envelope
```

- **Tool functions** live in `precis_mcp/tools/` (`read_tools.py`,
  `ingestion_tools.py`). Each module exposes a `register_*_tools(mcp, ref)`
  factory that registers its functions with the `@mcp.tool()` decorator — or
  `@register_mcp_tool(mcp)` from `precis_mcp/tools/_mcp_register.py` when the
  function takes a `_scope` parameter (see [Traps](#failure-modes-and-traps)).
- **`precis_mcp/dispatch.py`** is the single source of truth for tool
  metadata. `_load_open_tools()` calls the factories; `TOOL_CATALOGUE` holds
  one declarative entry per tool; `build_descriptors()` joins the two into a
  name → `ToolDescriptor` index and **raises at startup** for any loaded tool
  without a catalogue entry. `build_tool_json_schema()` derives the JSON
  Schema a client sees, and `process_tool_call()` is the pre-execution
  pipeline every call runs through.
- **`precis_mcp/mcp_external/server.py`** is the authenticated `/mcp`
  JSON-RPC transport (OIDC bearer tokens — see
  [Remote access](../deployment/oauth-keycloak.md)). It advertises the
  `mcp_read`-flagged subset of the catalogue, resolves variant names, runs the
  gate, executes the tool, and frames the result.
- **`precis_mcp/server.py`** is the single-user dev server (FastMCP over
  Streamable HTTP behind `MCP_DEV_KEY` — see the
  [quickstart](../getting-started/quickstart.md)).
  It calls the same `register_*_tools` factories directly, so a new module
  must be registered there too if you want it reachable on the dev server.

There is exactly one execution path. The catalogue, the gate, and the framing
are the three places a new tool touches.

## Invariants

- **Every loaded tool has a catalogue entry.** `build_descriptors` fails
  closed at startup otherwise — an uncatalogued tool would silently inherit
  `access="read"` and bypass the scenario-scope gate. A tool needing only the
  defaults still gets an explicit empty entry: `"my_tool": {}`.
- **Tools return a dict, always.** On failure return
  `{"error": "...", "error_type": "..."}` — never raise. The calling model
  reads the return value as its next context and can correct itself from a
  good error message.
- **The docstring is the model's documentation.** It is the fallback tool
  description on `tools/list` and the only thing the model knows about your
  parameters. Write it as if briefing a colleague who has never seen the code.
- **Never read identity inside the tool.** Declare `user_id: str = ""` and/or
  `_scope: dict | None = None` parameters; the wrapper injects them from the
  request's `AuthContext` and the gate's per-call scope. Both are stripped
  from the advertised schema (`_INJECTED_PARAMS` in `dispatch.py`), so a
  client can never supply them.
- **Enforce `_scope` against whatever you query.** The gate decides *whether*
  the caller may touch a scenario; your tool must apply the per-scenario
  domain/dimension allow/deny to the query itself. The engine does this for
  you when you pass `scope=_scope` into `execute_report`
  (`precis_mcp/engine/scope_enforcer.py` applies it); a tool that queries
  ClickHouse directly must honour it explicitly.
- **Results must be JSON-clean.** Datetimes, `Decimal`, `UUID`, NaN/Inf from
  pandas-backed paths — convert at the tool boundary.
  `_json_safe_inspection_result` in `read_tools.py` and `_serialise_load_rows`
  in `ingestion_tools.py` are the canonical converters.
- **Never leak internals in error messages.** Driver exceptions can embed SQL
  or filesystem paths. Log the detail, return a generic message — the
  `_safe_execute` helper in `read_tools.py` is the pattern.
- **`mcp_read` is opt-in.** A tool is advertised and callable over `/mcp` only
  if its catalogue entry sets `mcp_read: True` *and* its `access` is `read` or
  `general` (checked again at call time as defense-in-depth). The default
  `False` keeps the `access="read"` default from silently publishing every
  tool.

## The catalogue entry

`TOOL_CATALOGUE` in `precis_mcp/dispatch.py` maps tool name → metadata dict.
The fields, with their `ToolDescriptor` defaults:

| Field | Default | Purpose |
|---|---|---|
| `access` | `"read"` | Gate class. For this package the meaningful values are `read` (scenario-scoped, analyst+), `general` (no role/scope check at all — for tools with no per-scenario data; no open tool currently uses it), and `admin` (requires the `is_admin` user flag; e.g. `reload_integrations`). `write` / `plan_manager` exist in the model but no tool here uses them, and the `/mcp` transport refuses to execute anything that isn't `read` or `general`. |
| `mcp_read` | `False` | Publishes the tool over `/mcp` (`tools/list` and `tools/call`). Opt-in, independent of `access`. |
| `scenario_params` | `()` | Names of parameters that carry scenario references. Each referenced scenario is checked against the caller's per-scenario permissions, and the per-scenario `ScopeSpec` map is injected as `_scope`. Handles string params (`"scenario_id"`) and list-of-dict params (`"scenarios"` with `[{"scenario": "..."}]`). |
| `report_context` | `()` | Session-default injection keys (`filters`, `scenarios`, `scale`, …). Each key must name a real parameter on your signature — the schema is `extra='forbid'`, so injecting a key the function doesn't accept rejects the call. Unknown keys raise at startup. |
| `dimension_map_args` | `{}` | Maps a dict-shaped arg to a catalogue resolver (e.g. `{"filters": "report_domains"}`) for validation in `process_tool_call`. |
| `skills` | `set()` | Visibility tags consumed by the Précis platform's agent layer; inert over the `/mcp` transport. Leave it out. |

What `process_tool_call` does before your function runs, in order:

1. **Coercion** — params named in `_LIST_PARAMS` (`columns`, `dimensions`,
   `metrics`, `scenarios`) and `_DICT_PARAMS` (`filters`, `chart_spec`, …) are
   un-double-encoded when a model sends them as JSON strings, and list-of-str
   params are shape-checked with a corrective error. Name your parameters from
   these sets to inherit the coercion.
2. **Report-context injection** — per the `report_context` declaration.
3. **Permission gate** — `admin` → `is_admin` flag; `general` → pass;
   otherwise every scenario named in `scenario_params` is expanded to its
   underlying scenario ids (registry aliases, dependencies, fork targets) and
   checked against the caller's `permissions.scenarios[sid].tool_scopes`.
   On success the per-scenario scope map is placed on the `_call_scope`
   contextvar for injection. The gate runs **before** the catalogue-backed
   validations so a denied caller can't probe dimension members through
   validation errors.
4. **Dimension-map and dimension validation** against the metric catalogue.

## Worked example

A tool answering "which periods have successfully landed for a dataset?" —
read-only, queries the `load_history` audit table, no scenario scoping.

**1. Write the function.** New module `precis_mcp/tools/coverage_tools.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_coverage_tools(mcp: "FastMCP", catalogue_ref=None) -> None:
    """Register data-coverage tools on the MCP server instance."""

    @mcp.tool()
    def list_dataset_periods(dataset_id: str, limit: int = 24) -> dict:
        """List the accounting periods that have successfully landed for a dataset.

        Returns the distinct periods (YYYYMM, most recent first) with at least
        one successful load in `load_history`, up to `limit` periods.

        Use this to answer "is April in yet?" or "how far back does the GL go?"

        Args:
            dataset_id: e.g. 'gl' — the dataset to check.
            limit: period cap (default 24, max 120).

        Returns:
            {"dataset_id": ..., "periods": ["202604", ...], "count": N}.
            On error, {"error": "...", "error_type": "..."}.
        """
        from precis_mcp.db import query_platform

        if not dataset_id:
            return {"error": "dataset_id is required.", "error_type": "validation"}
        limit = max(1, min(int(limit), 120))
        rows = query_platform(
            """
            SELECT DISTINCT period FROM load_history
            WHERE dataset_id = %s AND status = 'success' AND period IS NOT NULL
            ORDER BY period DESC LIMIT %s
            """,
            (dataset_id, limit),
        )
        periods = [r["period"] for r in rows]
        return {"dataset_id": dataset_id, "periods": periods, "count": len(periods)}
```

Plain values only — strings and ints survive `json.dumps` as-is. If you select
timestamp or numeric columns, convert them (see `_serialise_load_rows`).

**2. Load it.** Add the factory call to `_load_open_tools()` in
`precis_mcp/dispatch.py`:

```python
from precis_mcp.tools.coverage_tools import register_coverage_tools
register_coverage_tools(reg, catalogue_ref)
```

And, if the tool should also be reachable on the single-user dev server, add
the same registration to `precis_mcp/server.py`.

**3. Catalogue entry.** In `TOOL_CATALOGUE`:

```python
"list_dataset_periods": {
    "mcp_read": True,
},
```

`access` stays at the `read` default; there are no scenario params, so the
gate passes the call through. Without `mcp_read: True` the tool would load but
never appear on `/mcp`.

**4. Description (optional but recommended).** `tools/list` falls back to the
docstring's first line. For a fuller, MCP-accurate description add an entry to
`_MCP_TOOL_DESCRIPTIONS` in `precis_mcp/mcp_external/framing.py`:

```python
"list_dataset_periods": (
    "List the accounting periods that have successfully landed for a "
    "dataset — most recent first."
),
```

**5. Done.** The tool is not in `_SPLIT_TOOLS` and not widget-linked, so it is
advertised as a single plain data tool (`out='agent'`): the model gets the raw
dict in both `content` and `structuredContent`. Nothing else to wire.

A **scenario-scoped** tool differs in two ways. The signature carries the
injected params and uses the registration shim:

```python
from precis_mcp.tools._mcp_register import register_mcp_tool

@register_mcp_tool(mcp)   # NOT @mcp.tool() — FastMCP rejects `_`-prefixed params
def my_scoped_tool(
    scenario_id: str,
    user_id: str = "",            # injected from AuthContext — hidden from clients
    _scope: dict | None = None,   # injected per-call scope — hidden from clients
) -> dict:
    ...
```

and the catalogue entry declares the scenario param:

```python
"my_scoped_tool": {
    "scenario_params": ("scenario_id",),
    "mcp_read": True,
},
```

`_scope` arrives as `{scenario_id: ScopeSpec | None}` (see
`precis_mcp/auth.py` — `ScopeSpec` carries domain and dimension allow/deny
axes; `None` means unrestricted). Pass it into `execute_report` or apply it to
your own query. `run_statement` / `run_metric` / `inspect_rows` in
`read_tools.py` are the live references for all of this.

## How `/mcp` advertises a tool

`precis_mcp/mcp_external/framing.py` owns the shape of the advertised surface.
An MCP host binds a widget to a tool *definition* — there is no per-call
output toggle — so one underlying tool can be advertised as several MCP names
with the output mode pinned per name:

- **`_SPLIT_TOOLS`** (`run_statement`, `run_metric`) are advertised twice: the
  bare name (`out='render'`, widget-linked) and `<name>_data` (`out='agent'`,
  raw figures). `mcp_tool_variants()` produces the list;
  `resolve_mcp_tool()` maps an advertised name back to
  `(underlying_tool, pinned_out)` at call time.
- **Widget-linked tools** (`_WIDGET_URI`: financial-table, inspection-grid,
  chart) carry `_meta.ui.resourceUri` pointing at a `ui://precis/<component>`
  resource, served from the built bundle in `ui/mcp-widgets/dist/`.
  `widget_uri_for()` only advertises a URI whose bundle is actually built. A
  widget-linked tool not in `_SPLIT_TOOLS` is render-only; everything else is
  a single raw-data tool.
- **Descriptions** come from `_MCP_TOOL_DESCRIPTIONS` (falling back to the
  docstring's first line), with `_variant_description()` in
  `mcp_external/server.py` appending the render-vs-data hint to split tools.
  The longer "how do I use this data model" guidance reaches the model through
  the synthetic `precis_orientation` tool's *result*
  (`mcp_external/instructions.py`) — hosted clients drop the `initialize`
  instructions field, so don't put load-bearing guidance there.
- **Dead params** (`_DEAD_MCP_PARAMS = {out, report_id, position}`) are
  stripped from every advertised `inputSchema` — `out` is pinned by the
  variant, and the other two belong to output paths the transport rejects.
- **Result framing** — `frame_tool_result()` builds the JSON-RPC envelope. Both
  `render` and `agent` return the raw engine result on `content` and
  `structuredContent`, so the model always reasons over stable figures; the
  `render` variant *additionally* carries its rendered display block on the
  result's `_meta` (`RENDER_BLOCK_META_KEY`) for the host to bind its widget to —
  widget-only, not shown to the model. `out='report'` raises `FramingError`.

For a plain data tool you touch none of this — the defaults advertise it as a
single raw-data tool. You only edit `framing.py` to add a description, and the
variant/widget machinery only matters if you build a widget for your tool.

### Extension seams

Several registration hooks exist so a downstream product can extend this
surface at startup without the open package importing it. In an open
deployment they are simply unregistered, and the corresponding behaviour is
absent:

- `dispatch.register_tool_loader(loader, catalogue_entries)` — add a batch of
  tools plus their catalogue metadata without editing `dispatch.py`. Also
  useful for your own tools if you'd rather keep them out of the package tree.
- `framing.register_mcp_tool_description(name, description)` — description
  override for a registered tool.
- `framing.register_mcp_output_mode(McpOutputMode)` — an extra split-tool
  variant with its own result framer (e.g. an `_excel` download variant);
  `is_available()` gates its advertisement.
- `server.register_mcp_render_builder(tool_name, builder)` — the
  render-block builder for a tool's `render` variant. The open transport
  registers the finance-table (`precis_mcp/table_builder.py`) and
  inspection-grid (`precis_mcp/inspection_grid_builder.py`) builders natively.
- `precis_mcp/read_tool_hooks.py` — hooks inside the read tools themselves:
  an output renderer for non-core `out` modes, a chart-result cache (mints
  the `data_ref`), and an Excel dispatcher. Unregistered, the read path
  returns figures with no `data_ref` and `out='excel'` / `out='report'` are
  unsupported.

(The Précis platform uses these same seams to attach its SPA block
emitters and richer output modes — that machinery is out of scope here.)

## Auth and scope — what a tool author must and must not do

**Must:**

- Declare `user_id: str = ""` if you need the caller's identity (audit rows,
  per-user filtering). It is injected from `get_auth_context().user_id`.
- Declare `_scope: dict | None = None` *and* `scenario_params` in the
  catalogue if the tool reads scenario-bearing data, then enforce the scope
  against the query. Declaring the param without the catalogue entry means the
  gate never runs and nothing is injected — a silent authorization hole.
- Filter visibility yourself when returning per-scenario metadata: the gate
  only checks scenarios named in the *arguments*. `list_scenarios` in
  `read_tools.py` shows the pattern — it drops rows the caller's
  `permissions.scenarios[sid].tool_scopes` doesn't grant `read` on.
- Use `@register_mcp_tool(mcp)` for any function with a `_`-prefixed
  parameter.

**Must not:**

- Call `get_auth_context()` / `get_call_scope()` for things the injected
  params already give you — the contextvars are transport plumbing, set per
  request and cleared in the transport's `finally` block. (Reading
  `get_auth_context().permissions` for visibility filtering, as
  `list_scenarios` does, is the one legitimate direct use.)
- Cache scope or identity across calls in module state.
- Accept `user_id` or `_scope` from the client — you can't; the schema
  derivation strips them. Don't try to re-add them under another name either.
- Write. No INSERTs into anything except best-effort audit rows, no
  filesystem output. (`security_audit_log` and `inspection_audit`
  writes follow the "log on failure, never block the response" pattern.)

## Testing

Tests live in the same repo, split by class:

- **`tests/unit/`** — pure logic, no database, no network. Helpers like
  serialisers and validators belong here (e.g.
  `tests/unit/test_read_tools_json_safe.py`).
- **`tests/component/`** — register your factory against
  `tests/fakes/mock_mcp.MockMCP` (an in-memory FastMCP substitute that
  captures `@mcp.tool()` registrations into a dict) and substitute the
  backends with the shared fakes: `tests/fakes/fake_clickhouse.py` and
  `tests/fakes/fake_platform_db.py`, plus the
  factories in `tests/factories/`. `tests/component/test_ingestion_read_tools.py`
  is a complete template — fake platform DB monkeypatched in, factory
  registered on `MockMCP`, tool invoked as a plain function.
- **`tests/open_tests.txt`** — the manifest of test files that belong to this
  package; it is what defines the suite. Add your new test file to it. The
  membership rule: a listed test imports only from the `precis_mcp` tree and
  the shared test infrastructure (`tests/fakes/`, `tests/factories/`); a
  collection-time guard in `tests/conftest.py` fails the run on violations.

A useful component test for a new tool covers at minimum: the happy path, the
`{"error": ...}` path for bad input, and (for scoped tools) that the scope
passed as `_scope` actually constrains the result.

## Failure modes and traps

- **`@mcp.tool()` on a `_scope`-bearing function crashes the server at
  startup.** FastMCP raises `InvalidSignature` for `_`-prefixed parameters at
  decoration time. `MockMCP`-based tests will *not* catch this — they
  duck-type the decorator. Use `@register_mcp_tool(mcp)`, which strips hidden
  params from the FastMCP-exposed signature, and smoke-test registration
  against a real `FastMCP` instance if in doubt.
- **Missing catalogue entry → startup failure.** Deliberate
  (`build_descriptors` fails closed). The error message names the tool.
- **Forgotten `mcp_read: True` → tool invisible on `/mcp`.** It loads, the
  dev server serves it, but the external transport answers
  "Tool not exposed over MCP". Conversely, flagging `mcp_read` on an `admin`
  tool gets it advertised but every call rejected — the call-time gate
  requires read-class access.
- **`report_context` keys that aren't real parameters reject every call.**
  The advertised schema is `extra='forbid'`; an injected key the function
  doesn't accept fails validation. Unknown key *names* are caught earlier, at
  startup.
- **NaN/Inf in results.** Pandas maps SQL NULL to `float('nan')` via
  `DataFrame.to_dict("records")`; strict JSON consumers downstream reject it.
  Sanitize at the tool boundary — `_json_safe_inspection_result` is the
  canonical converter.
- **Raw exception text in errors.** Driver exceptions embed SQL and paths.
  Catch broadly, log with `logger.exception`, return
  `"...failed with an internal {type}; details were logged."` — the
  `_safe_execute` pattern.
- **Scenario params the gate can't see.** `_extract_scenario_ids` only reads
  the parameter names listed in `scenario_params`. Rename a parameter without
  updating the catalogue and the permission check silently stops applying.
- **List/dict params outside the coercion sets.** Models sometimes
  double-encode structured args as JSON strings. Only params named in
  `_LIST_PARAMS` / `_DICT_PARAMS` (in `dispatch.py`) get un-mangled; a
  custom-named dict param receives the raw string and your tool must cope or
  reject.

## Do / Don't

| Do | Don't |
|---|---|
| Return `{"error": "...", "error_type": "..."}` on failure | Raise exceptions out of the tool |
| Write a precise, complete docstring | Rely on the parameter names to explain themselves |
| Add one `TOOL_CATALOGUE` entry per tool (empty dict is valid) | Scatter metadata, or skip the entry (startup fails) |
| Declare `user_id` / `_scope` params and let the wrapper inject them | Read the auth contextvars for identity decisions inside the tool |
| Pass `scope=_scope` into `execute_report`, or enforce it on your own query | Treat the gate's scenario check as full enforcement |
| Use `@register_mcp_tool(mcp)` for `_`-prefixed params | Use `@mcp.tool()` and find out at server startup |
| Convert datetimes/Decimal/NaN at the tool boundary | Trust the backend's types to be JSON-clean |
| Set `mcp_read: True` deliberately, per tool | Assume `access="read"` publishes the tool |
| Keep error messages generic; log the detail | Echo driver exceptions to the client |

## Pre-flight checklist

- [ ] Tool function in `precis_mcp/tools/<domain>_tools.py`, registered by a
      `register_*_tools(mcp, ref)` factory
- [ ] Correct decorator: `@mcp.tool()`, or `@register_mcp_tool(mcp)` if the
      signature has `_`-prefixed params
- [ ] Returns a dict; error path returns `{"error", "error_type"}`; output is
      JSON-clean
- [ ] Factory wired into `_load_open_tools()` in `precis_mcp/dispatch.py`
      (and `precis_mcp/server.py` if the dev server should serve it)
- [ ] `TOOL_CATALOGUE` entry with the right `access`, `scenario_params`, and
      `mcp_read`
- [ ] Scoped tools: `_scope` declared **and** enforced against the query
- [ ] Description added to `_MCP_TOOL_DESCRIPTIONS` in
      `mcp_external/framing.py` (or the docstring's first line is good enough)
- [ ] Component test on `MockMCP` + fakes in `tests/component/`; pure helpers
      in `tests/unit/`
- [ ] New test files added to `tests/open_tests.txt`

## What this guide doesn't cover

- **Defining metrics, statements, and dimensions** — that's catalogue
  configuration, not code: [Catalogue & semantic model](../configuration/catalogue-and-semantic.md).
- **Loading data** — [Ingestion & data sources](../configuration/ingestion.md).
- **Standing up the transports** — [quickstart](../getting-started/quickstart.md)
  for the dev server, [remote access](../deployment/oauth-keycloak.md) for the
  authenticated `/mcp` endpoint.
- **Anything that writes.** Précis-MCP is the read surface; there is no
  supported path for a write tool in this package.
