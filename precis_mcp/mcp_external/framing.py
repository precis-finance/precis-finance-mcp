# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""`out=` framing for the external MCP transport.

Produces the JSON-RPC `tools/call` result envelope:

| out=            | structuredContent | content (text)         |
|-----------------|-------------------|------------------------|
| agent / render  | engine result     | engine result as JSON  |
| excel           | —                 | filename + base64 XLSX |
| report          | rejected — Précis report builder is SPA-only       |

`render` and `agent` produce the same envelope. A widget, when one is built
for the tool, is linked per the MCP Apps extension
(`io.modelcontextprotocol/ui`): the tool definition in `tools/list` carries
`_meta.ui.resourceUri`, and the host fetches the bundle via `resources/read`
— it is NOT inlined in the result. The resource handlers and the
tools/list `_meta` live in `precis_mcp/mcp_external/server.py`; this module
owns the result envelope and the bundle-lookup helpers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# MCP Apps (io.modelcontextprotocol/ui) MIME for ui:// HTML resources.
MCP_APP_MIME = "text/html;profile=mcp-app"


# Maps block-emitting tools to the widget bundle URI.  Populated as
# bundles ship in `ui/mcp-widgets/`.  Keyed by the *render-variant* MCP name
# (see `mcp_tool_variants`) — the `_data` variants are absent here, so they
# carry no `_meta.ui` and the host renders no widget for them.
_WIDGET_URI: dict[str, str] = {
    "run_metric": "ui://precis/financial-table",
    "run_statement": "ui://precis/financial-table",
    "inspect_rows": "ui://precis/inspection-grid",
    "eval_chart_transform": "ui://precis/chart",
}


# Tools exposed over MCP as two variants. The host binds a widget to a tool
# *definition* — there is no per-call render toggle like the SPA's `out=` — so
# a tool whose output mode the model should choose between is advertised as the
# bare name (`out='render'`, widget-linked) plus a `<name>_data` variant
# (`out='agent'`, no widget). Tools not listed here are single: widget-linked
# ones (`_WIDGET_URI`) are render-only; everything else is a plain data tool.
_SPLIT_TOOLS: frozenset[str] = frozenset({"run_statement", "run_metric"})
_DATA_SUFFIX = "_data"


@dataclass(frozen=True)
class McpOutputMode:
    """A commercial extension adding a split-tool variant + its result framer.

    The open transport ships the `render` + `_data` variants natively; richer
    output modes (Excel download) require the file subsystem and are injected by
    the commercial product at startup via `register_mcp_output_mode`. An open
    deployment registers none, so it advertises neither the variant nor frames
    its `out`.

    - `out` — the pinned `out=` value the variant maps to (e.g. `'excel'`).
    - `suffix` — the advertised-name suffix (e.g. `'_excel'`).
    - `framer(tool_name, result) -> mcp_result_dict` — turns the tool result
      into a JSON-RPC `tools/call` envelope.
    - `is_available()` — gates advertisement (e.g. Excel needs a signing key).
    """

    out: str
    suffix: str
    framer: Callable[[str, Any], dict]
    is_available: Callable[[], bool] = field(default=lambda: True)


_OUTPUT_MODES: dict[str, McpOutputMode] = {}


def register_mcp_output_mode(mode: McpOutputMode) -> None:
    """Register a commercial output mode (variant + framer). Keyed by `out`."""
    _OUTPUT_MODES[mode.out] = mode


def unregister_mcp_output_mode(out: str) -> None:
    """Drop a registered output mode (test isolation; no-op if absent)."""
    _OUTPUT_MODES.pop(out, None)


def mcp_tool_variants(tool_name: str) -> list[tuple[str, str]]:
    """`(mcp_name, pinned_out)` pairs an underlying tool is advertised as.

    Split tools → a render variant (bare name, widget), a `_data` variant
    (`out='agent'`, no widget), plus one variant per registered+available
    output mode (commercial; e.g. `_excel`). A widget-linked tool that isn't
    split is render-only. Everything else is a single `agent` (raw-data) tool.
    """
    if tool_name in _SPLIT_TOOLS:
        variants = [
            (tool_name, "render"),
            (tool_name + _DATA_SUFFIX, "agent"),
        ]
        for mode in _OUTPUT_MODES.values():
            if mode.is_available():
                variants.append((tool_name + mode.suffix, mode.out))
        return variants
    if tool_name in _WIDGET_URI:
        return [(tool_name, "render")]
    return [(tool_name, "agent")]


def resolve_mcp_tool(mcp_name: str) -> tuple[str, str]:
    """Map an advertised MCP tool name back to `(underlying_tool, pinned_out)`."""
    if mcp_name.endswith(_DATA_SUFFIX) and mcp_name[: -len(_DATA_SUFFIX)] in _SPLIT_TOOLS:
        return (mcp_name[: -len(_DATA_SUFFIX)], "agent")
    for mode in _OUTPUT_MODES.values():
        if mcp_name.endswith(mode.suffix) and mcp_name[: -len(mode.suffix)] in _SPLIT_TOOLS:
            return (mcp_name[: -len(mode.suffix)], mode.out)
    if mcp_name in _WIDGET_URI:
        return (mcp_name, "render")
    return (mcp_name, "agent")


# MCP-accurate tool descriptions. The SPA docstrings document `out=`, the report
# builder, HITL, and the active report context — all false on the read-only MCP
# variant surface — so dumping them would mislead the host model, and a blunt
# first-line clip is too thin. These concise overrides describe the tool's
# *purpose* (the output mode is conveyed by the variant suffix, see
# `_variant_description`). Keyed by the underlying tool name.
_MCP_TOOL_DESCRIPTIONS: dict[str, str] = {
    "run_statement": (
        "Run a financial statement — P&L, variance report, or executive "
        "summary. Rows are statement lines (Revenue, Direct Cost, Gross Margin, "
        "…); columns are scenarios. Supports an optional dimension breakdown "
        "(e.g. by period or cost centre)."
    ),
    "run_metric": (
        "Break one or more metrics down by a dimension — revenue by project, "
        "utilisation by employee, headcount trends, GL account drill-down. Rows "
        "are the dimension; columns are metrics × scenarios. Pass `scenario_id` "
        "explicitly (usually `actuals`)."
    ),
    "inspect_rows": (
        "Inspect the row-level detail behind a figure, from an enabled "
        "inspection source. Returns a capped sample for reasoning plus a grid "
        "for the user."
    ),
    "list_scenarios": "List the available planning scenarios and their status.",
    "list_kpis": (
        "Browse the metric catalogue — metric keys, formats, domains, and the "
        "dimensions available per metric."
    ),
    "search_hierarchy": (
        "Search the dimension hierarchies (cost centres, accounts, …) to find "
        "valid codes and ids before composing a query."
    ),
    "list_inspection_sources": "List the row-level sources available for inspection.",
    "get_inspection_schema": "Get the column schema for an inspection source.",
    "list_variants": "List the what-if variants of a scenario.",
    "list_load_history": (
        "List data-load attempts from the ingestion audit trail — when each "
        "dataset landed, with what status. Answers \"is April in yet?\" / "
        "\"when was this data last loaded?\"."
    ),
    "get_load_status": (
        "Fetch one data load's full detail by load_id — timestamps, status, "
        "rows landed, and any error message."
    ),
    "list_bindings": (
        "List the configured data feeds (ingestion bindings) with their "
        "schedule — which datasets load, from where, how often."
    ),
    "get_binding": (
        "Fetch one data feed's full configuration: source, target dataset, "
        "schedule, and extract parameters."
    ),
}


# Commercial description overrides — a commercial tool's MCP-accurate
# description, registered at startup. Kept out of the open `_MCP_TOOL_DESCRIPTIONS`
# so the open package carries no description for a tool it never advertises
# (e.g. `eval_chart_transform`). Same seam shape as `register_mcp_output_mode`.
_DESCRIPTION_OVERRIDES: dict[str, str] = {}


def register_mcp_tool_description(tool_name: str, description: str) -> None:
    """Register a commercial tool's MCP description. Idempotent; last wins."""
    _DESCRIPTION_OVERRIDES[tool_name] = description


def unregister_mcp_tool_description(tool_name: str) -> None:
    """Drop a registered description (test isolation; no-op if absent)."""
    _DESCRIPTION_OVERRIDES.pop(tool_name, None)


def mcp_tool_description(tool_name: str) -> str | None:
    """The MCP-accurate base description for a tool, or None to fall back to the
    docstring's first line. A commercial override (registered at startup) wins
    over the open table."""
    if tool_name in _DESCRIPTION_OVERRIDES:
        return _DESCRIPTION_OVERRIDES[tool_name]
    return _MCP_TOOL_DESCRIPTIONS.get(tool_name)


class FramingError(RuntimeError):
    """Raised when a tool's out= choice can't be framed for MCP."""


def _bundle_path_for_uri(uri: str) -> Path | None:
    """Filesystem path for a widget `ui://` URI, or None if not a known widget.

    Guards against path traversal: only URIs registered in `_WIDGET_URI`
    resolve — `resources/read` takes a client-supplied URI, so this must
    never read an arbitrary file.
    """
    if uri not in _WIDGET_URI.values():
        return None
    component = uri.rsplit("/", 1)[-1]
    return (
        Path(__file__).resolve().parent.parent.parent
        / "ui" / "mcp-widgets" / "dist" / f"{component}.html"
    )


def widget_uri_for(tool_name: str) -> str | None:
    """The `ui://` URI for a tool whose widget bundle is built, else None.

    Used by `tools/list` to stamp `_meta.ui.resourceUri`. Returns None when
    the tool has no widget or its bundle hasn't been built — so the host is
    never pointed at a resource that `resources/read` can't serve.
    """
    uri = _WIDGET_URI.get(tool_name)
    if not uri:
        return None
    path = _bundle_path_for_uri(uri)
    return uri if (path is not None and path.is_file()) else None


def read_widget_bundle(uri: str) -> str | None:
    """HTML for a built widget `ui://` URI, else None (serves `resources/read`)."""
    path = _bundle_path_for_uri(uri)
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


# Sandbox-isolation origin base for ChatGPT widget iframes. Each widget gets a
# unique per-component origin (`<component>.widgets.precis.finance`); it is NOT
# a real host — ChatGPT uses it only to sandbox the iframe and requires it set.
_WIDGET_DOMAIN_BASE = "widgets.precis.finance"


def _claude_widget_domain(server_url: str) -> str | None:
    """claude.ai's sandbox origin for our widgets: the bare host
    `<sha256(server_url)[:32]>.claudemcpcontent.com`.

    claude.ai validates `_meta.ui.domain` against this exact shape and rejects
    anything else (a custom `https://` host 400s). The hash input must be the
    MCP server URL claude.ai connected with, byte-for-byte — derived here from
    `PRECIS_BASE_URL/mcp` (the same string `oidc.mcp_audience` derives), so it
    tracks the deployment. Returns None when no base URL is configured (dev),
    in which case the key is omitted and claude.ai falls back to its default.
    """
    if not server_url:
        return None
    digest = hashlib.sha256(server_url.encode()).hexdigest()[:32]
    return f"{digest}.claudemcpcontent.com"


def widget_resource_meta(uri: str, *, server_url: str = "") -> dict:
    """`_meta.ui` for a widget resource (the `resources/read` response).

    Per the MCP Apps / OpenAI Apps SDK contract, hosts require a CSP and a
    unique domain on the template or they refuse to render the widget. Our
    widgets are self-contained (vite-singlefile: JS + CSS inlined, no external
    fetches; host comms over postMessage), so the CSP domain lists are empty.

    `domain` is per-host and the two hosts read different keys:
    - `openai/widgetDomain` (ChatGPT) — synthetic per-component origin.
    - `ui.domain` (claude.ai) — `<hash>.claudemcpcontent.com`, hashed from the
      MCP server URL; validated strictly, so we never send the ChatGPT value
      here. Omitted when no server URL is known (dev).
    """
    component = uri.rsplit("/", 1)[-1]
    openai_domain = f"https://{component}.{_WIDGET_DOMAIN_BASE}"
    ui: dict[str, Any] = {"csp": {"connectDomains": [], "resourceDomains": []}}
    claude_domain = _claude_widget_domain(server_url)
    if claude_domain:
        ui["domain"] = claude_domain
    return {
        "ui": ui,
        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
        "openai/widgetDomain": openai_domain,
    }


def list_widget_resources() -> list[dict]:
    """Built widget resources for `resources/list` (deduped by URI)."""
    out: list[dict] = []
    seen: set[str] = set()
    for uri in _WIDGET_URI.values():
        if uri in seen or read_widget_bundle(uri) is None:
            continue
        seen.add(uri)
        out.append({
            "uri": uri,
            "name": uri.rsplit("/", 1)[-1],
            "mimeType": MCP_APP_MIME,
        })
    return out


def frame_tool_result(
    *,
    tool_name: str,
    out: str,
    tool_result: Any,
    render_block: dict | None = None,
) -> dict:
    """Produce the JSON-RPC `tools/call` result envelope.

    Raises `FramingError` for unsupported framings (currently `out=report`).

    `out` is pinned per advertised tool variant (see `mcp_tool_variants`), so a
    given MCP tool is always one mode:

    - `out='render'` (widget variant) — `structuredContent` is the emitted block
      the widget binds to (falling back to the raw result if no block was
      derived). The model reads the same block as JSON in `content`, so it has
      the figures whether or not the host draws the widget. No presentation
      instruction is added: the hosted clients (claude.ai, ChatGPT) render the
      widget and inject their own "don't repeat it" guidance, and our MCP tool
      descriptions don't claim "renders in the UI" — so an extra nudge only
      contradicts the host and reads as injected guidance.
    - `out='agent'` (`_data` variant) — `structuredContent` and `content` are
      the raw engine result; no widget is linked, so the model presents text.

    The widget HTML itself is linked via the tool's `_meta.ui` in `tools/list`
    and fetched by `resources/read`, never inlined in the result.
    """
    if out == "report":
        raise FramingError(
            "out='report' is rejected on the MCP transport — the Précis "
            "report builder is SPA-only.  Use out='render' or out='excel'."
        )
    mode = _OUTPUT_MODES.get(out)
    if mode is not None:
        return mode.framer(tool_name, tool_result)

    if out == "render":
        block = render_block if render_block is not None else (
            tool_result if isinstance(tool_result, dict) else {"value": tool_result}
        )
        text = json.dumps(block, default=str, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": block,
            "isError": False,
        }

    # agent / unset — raw result on both channels; no widget.
    raw = tool_result if isinstance(tool_result, dict) else {"value": tool_result}
    text = json.dumps(raw, default=str, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": raw,
        "isError": False,
    }
