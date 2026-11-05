# Adding read tools

Précis Finance MCP exposes your financial model to MCP clients through a small set of
read-only tools — `run_statement`, `run_metric`, `inspect_rows`, the
discovery tools, and the ingestion-status reads (the advertised set is
catalogued in the [MCP tool reference](../reference/mcp-tools.md)). This
guide is the contract for adding a tool of your own: where the function
lives, how it gets registered and advertised, what the permission gate does
for you, and what your tool must still do itself.

Everything here stays inside the `precis_mcp` package. Financial access is
read-only: new financial tools query the analytical backend, catalogue or
platform database. Artifact tools follow the separate owned-file contract
described under [Extension seams](#extension-seams); they cannot mutate finance data.

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
  factory that registers its functions with plain `@mcp.tool()`, including
  functions with injected `_scope` parameters. Preserve their full signatures
  for the production registry (see [Traps](#failure-modes-and-traps)).
  Financial reads acquire the selected analytical client and use the existing
  catalogue reference. Build SQL through that client's dialect; do not construct
  raw CH clients or edit published
  catalogue objects in place. Stored financial definitions replay through
  `get_financial_read_dispatch()` and the same permission/scope gate. See
  [Analytical client](../configuration/analytical-client.md).
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
  opted-in read and artifact tools, resolves variant names, runs the
  gate, executes the tool, and frames the result.
- **`precis_mcp/server.py`** is the single-user dev server (FastMCP over
  Streamable HTTP behind `MCP_DEV_KEY` — see the
  [quickstart](../getting-started/quickstart.md)).
  It calls the same factories through `DevMcpRegistrar` from
  `precis_mcp/tools/_dev_mcp.py`, which hides injected parameters from FastMCP.
  Register new modules there too if they should be reachable on the dev server.

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
- **Remote publication is opt-in.** Reads require `mcp_read: True` and
  `access` of `read` or `general`. Artifact mutations require
  `mcp_artifact: True`, `access="general"` and the explicit artifact-tool
  allow-list. These conditions are checked at call time; access class alone
  never publishes a tool.

## The catalogue entry

`TOOL_CATALOGUE` in `precis_mcp/dispatch.py` maps tool name → metadata dict.
The fields, with their `ToolDescriptor` defaults:

| Field | Default | Purpose |
|---|---|---|
| `access` | `"read"` | Gate class: `read` (scenario-scoped, analyst+), `general` (no scenario-role gate; used by chart and file tools), or `admin` (requires `is_admin`; e.g. `reload_integrations`). General tools still enforce their owner, quota and current-grant checks. The external `/mcp` transport accepts only `read` or `general`, with a separate publication flag. |
| `mcp_read` | `False` | Opts a read tool into `/mcp`; callable only with `read` or `general` access. |
| `mcp_artifact` | `False` | Opts an artifact mutation into `/mcp`; callable only with `general` access and membership in `tools/artifact_tools.py::ARTIFACT_MUTATING_TOOLS`. External receives also require upload opt-in; see [Extension seams](#extension-seams). |
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

For the single-user dev server, add the registration to `precis_mcp/server.py`,
passing `DevMcpRegistrar(mcp)` instead of the raw FastMCP instance.

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
injected params, still using the same decorator inside its factory:

```python
@mcp.tool()
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

- **`_SPLIT_TOOLS`** (`run_statement`, `run_metric`, `inspect_rows`) are advertised twice: the
  bare name (`out='render'`, widget-linked) and `<name>_data` (`out='agent'`,
  raw figures). `mcp_tool_variants()` produces the list;
  `resolve_mcp_tool()` maps an advertised name back to
  `(underlying_tool, pinned_out)` at call time.
- **Widget-linked tools** (`_WIDGET_URI`: financial-table, inspection-grid,
  chart) carry `_meta.ui.resourceUri` pointing at a `ui://precis/<component>`
  resource, served from the built bundle in `precis_mcp/mcp_external/widgets/`.
  `widget_uri_for()` only advertises a URI whose bundle is actually built. A
  widget-linked tool not in `_SPLIT_TOOLS` is render-only; everything else is
  a single raw-data tool.
- **Descriptions** come from `_MCP_TOOL_DESCRIPTIONS` (falling back to the
  docstring's first line), with `_variant_description()` in
  `mcp_external/server.py` appending the render-vs-data hint to split tools.
  The longer "how do I use this data model" guidance reaches the model through
  the synthetic `precis_orientation` tool's *result*
  (`mcp_external/instructions.py`) — hosted clients drop the `initialize`
  instructions field, so don't put load-bearing guidance there. When argument
  discipline affects what the user sees, keep the short description and
  orientation aligned; the reporting tools, for example, require the calling
  model to give every scenario a user-facing display `alias`.
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

The downstream agent loader (`dispatch.register_tool_loader`) adds workflow tools to the agent assembly. The remote transport deliberately assembles core descriptors only. Add reusable remote tools to the core registration and catalogue; downstream commercial startup must not change that surface. Shared computation and presentation live in these modules:

- `precis_mcp/datasets.py` — bounded owner/grant-bound snapshots. Cache the result before consumer rounding or preview truncation. A failed cache must leave a successful finance read usable.
- `precis_mcp/tools/chart_tools.py` and `precis_mcp/chart_builder.py` — shared chart computation and render-block derivation.
- `precis_mcp/artifacts/` and `tools/artifact_tools.py` — shared owned file storage, transfer and snapshot exports. Artifact mutation requires `mcp_artifact=True`, `access="general"`, and the explicit transport allow-list; this is not permission to expose financial writes. There is no separate artifact access class, and general access alone does not publish a tool. Owner, quota and grant-stamp checks remain mandatory. External receives additionally require `PRECIS_MCP_UPLOADS_ENABLED=true`; keep new receiving tools in `artifacts/policy.py::EXTERNAL_UPLOAD_TOOLS` so both discovery and direct dispatch honor the default-off policy. Attachment import also needs its hostname allow-list. Exports and downloads bypass the receiving policy.

Remote descriptors always assemble the core tool set. Do not install output-mode, chart-builder or instruction overrides from a downstream product. Keep workflow persistence outside core computation.

## Auth and scope — what a tool author must and must not do

**Must:**

- Declare `user_id: str = ""` if you need the caller's identity (audit rows,
  per-user filtering). It is injected from `get_auth_context().user_id`.
- Declare `_scope: dict | None = None` *and* `scenario_params` in the
  catalogue if the tool reads scenario-bearing data, then enforce the scope
  against the query. Declaring the param without the catalogue entry means the
  gate never runs and nothing is injected — a silent authorization hole.
- Filter visibility when returning per-scenario metadata: the gate only checks
  scenarios named in the *arguments*. Reuse
  `scenario_registry.readable_scenario_ids(permissions)` for the readable real
  IDs and `filter_reporting_vocabulary(...)` when the result also contains
  shifted/comparison keys. `list_scenarios` in `read_tools.py` is the canonical
  pattern; do not reimplement the membership/dependency rules in each tool.
- Read current permissions at each call. `refresh_permissions()` in
  `precis_mcp.auth` can reload the authenticated caller's account/profile and
  update the request's shared permissions object after a resource mutation.
  Do not retain a separate copy of its scenario map: replacement of that map
  makes the copy stale. A failed reload clears cached grants and raises;
  absent auth context is the explicit dev path and returns `None`.
- Use plain `@mcp.tool()` in shared factories; pass `DevMcpRegistrar(mcp)`
  when registering them with the dev server's FastMCP instance.

**Must not:**

- Call `get_auth_context()` / `get_call_scope()` for things the injected
  params already give you — the contextvars are transport plumbing, set per
  request and cleared in the transport's `finally` block. (Reading
  `get_auth_context().permissions` to feed the shared visibility helpers, as
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

- **Passing raw FastMCP to a scoped factory crashes dev-server registration.**
  FastMCP rejects `_`-prefixed parameters. Use `DevMcpRegistrar(mcp)` at that
  boundary; keep plain `@mcp.tool()` and full signatures in shared factories
  so production can detect and inject scope. `MockMCP`-based tests do not catch
  the raw-FastMCP failure.
- **Missing catalogue entry → startup failure.** Deliberate
  (`build_descriptors` fails closed). The error message names the tool.
- **Forgotten `mcp_read: True` → read tool invisible on `/mcp`.** It loads, the
  dev server serves it, but the external transport answers
  "Tool not exposed over MCP". Conversely, flagging `mcp_read` on an `admin`
  tool gets it advertised but every call rejected — the call-time gate
  requires `read` or `general` access. Artifact mutations instead use the
  separate `mcp_artifact` contract.
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
| Use plain `@mcp.tool()` factories and `DevMcpRegistrar` at the dev-server boundary | Strip injected params before production registration or pass raw FastMCP to scoped factories |
| Convert datetimes/Decimal/NaN at the tool boundary | Trust the backend's types to be JSON-clean |
| Select `mcp_read` or `mcp_artifact` under its transport contract | Assume an access class publishes the tool |
| Keep error messages generic; log the detail | Echo driver exceptions to the client |

## Pre-flight checklist

- [ ] Tool function in `precis_mcp/tools/<domain>_tools.py`, registered by a
      `register_*_tools(mcp, ref)` factory
- [ ] Plain `@mcp.tool()` in the factory; dev-server registration uses
      `DevMcpRegistrar(mcp)` to hide injected parameters from FastMCP
- [ ] Returns a dict; error path returns `{"error", "error_type"}`; output is
      JSON-clean
- [ ] Factory wired into `_load_open_tools()` in `precis_mcp/dispatch.py`
      (and `precis_mcp/server.py` if the dev server should serve it)
- [ ] `TOOL_CATALOGUE` entry with the right `access`, `scenario_params`, and
      publication flag (`mcp_read`, or `mcp_artifact` under its separate contract)
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
- **Financial writes.** There is no supported path for a financial write tool
  in this package. Owned artifact writes follow the separate contract above.
