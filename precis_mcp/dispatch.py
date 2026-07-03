# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tool dispatch core — single source of truth for tool metadata and the gate.

Open-core module shared by both transports (the LangGraph agent and the
external MCP server). It owns the ``ToolDescriptor`` model, the **open**
``TOOL_CATALOGUE`` and open tool loaders, tool wrapping, the LLM-facing
signature derivation, and the pre-execution permission gate. It has no
dependency on the agent runtime (LangChain/LangGraph) or on any Précis
tool module — those LLM-binding concerns live in the Précis agent's
tool-registry layer, and the Précis tool *set* is injected
via :func:`register_tool_loader`.

The open package ships only the read-only tool set (metric/statement/inspect,
list/search, ingestion reads, validation). The Précis platform
registers its additional tools + catalogue metadata at startup; an open
deployment registers none, so ``build_descriptors`` returns the open set alone.

Provides:

- ``register_tool_loader()`` — Précis extension point: add a tool loader +
  its catalogue metadata (the open package registers none)
- ``build_descriptors()`` — load tools (open + registered), build the
  name→ToolDescriptor index
- ``build_tool_json_schema()`` — JSON Schema for a tool's LLM-facing args
  (Pydantic-native; the MCP ``tools/list`` surface consumes it)
- ``process_tool_call()`` — pre-execution pipeline (context injection,
  validation, permissions)
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ConfigDict, create_model

from precis_mcp.auth import (
    clear_call_scope,
    get_auth_context,
    get_call_scope,
    set_call_scope,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolDescriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDescriptor:
    """Single source of truth for a tool's agent-facing metadata."""

    name: str
    func: Callable  # the raw tool function (before wrapping)
    skills: frozenset[str] = frozenset()  # empty = base tool (always visible)
    access: str = "read"  # "read" | "write" | "plan_manager" | "admin" | "general"
    # Exposed over the external `/mcp` transport.  Opt-in (default False):
    # `access` gates the agent-facing role check; `mcp_read` is a separate
    # transport-level decision about what an external MCP client may see.
    # Keeping it explicit stops the `access="read"` default from silently
    # publishing every uncatalogued tool to the connector.
    mcp_read: bool = False
    scenario_params: tuple[str, ...] = ()  # args that carry scenario IDs
    # Scenario injected before the permission gate when a scenario-scoped read
    # tool is called with none. The tool's own in-body default lands *after*
    # the gate, so without this the gate sees no scenario, takes the read-class
    # pass-through, and the tool then queries its default unscoped. Empty = no
    # injected default (write/plan_manager tools must name a scenario; the gate
    # denies one that omits it).
    default_scenario: str = ""
    # Report-context keys to inject on this tool. Each entry must name a
    # real parameter on the tool's signature; otherwise Pydantic's
    # extra='forbid' will reject the call. Empty tuple = no injection.
    # Allowed keys: "filters", "statement", "scenarios", "scale",
    # "decimals", "period_start", "period_end".
    report_context: tuple[str, ...] = ()
    # Maps arg-name -> target-resolver key.  E.g. {"filters": "report_domains"}
    # or {"commit_scope": "plan_datasets"}.  Drives generic dimension-map
    # validation in process_tool_call step 3.
    dimension_map_args: dict[str, str] = field(default_factory=dict)


_VALID_REPORT_CONTEXT_KEYS: frozenset[str] = frozenset({
    "filters", "statement", "scenarios", "scale", "decimals",
    "period_start", "period_end",
})


# Canonical injection set for read-time tools (run_statement / run_metric).
# Defined here so the catalogue stays terse and the two tools that share
# this contract can't drift apart.
_FULL_READ_CONTEXT: tuple[str, ...] = (
    "filters", "scenarios", "scale", "decimals", "period_start", "period_end",
)


# ---------------------------------------------------------------------------
# TOOL_CATALOGUE — declarative metadata overrides
# ---------------------------------------------------------------------------
# Tools not listed here get default ToolDescriptor values (base tool,
# read access, no scenario params, no report context).
#
# Adding a new tool: add ONE entry here.  That's it.

TOOL_CATALOGUE: dict[str, dict[str, Any]] = {
    # --- read tools (skill-gated, receive report context) ---
    "run_statement": {
        "skills": {"reporting", "analysis", "planning", "excel_workflow", "routines"},
        "scenario_params": ("scenarios",),
        "default_scenario": "actuals",
        "report_context": ("statement",) + _FULL_READ_CONTEXT,
        "dimension_map_args": {"filters": "report_domains"},
        "mcp_read": True,
    },
    "run_metric": {
        "skills": {"reporting", "analysis", "planning", "excel_workflow", "routines"},
        "scenario_params": ("scenarios",),
        "default_scenario": "actuals",
        "report_context": _FULL_READ_CONTEXT,
        "dimension_map_args": {"filters": "report_domains"},
        "mcp_read": True,
    },
    "list_inspection_sources": {
        "skills": {"reporting", "analysis"},
        "mcp_read": True,
    },
    "get_inspection_schema": {
        "skills": {"reporting", "analysis"},
        "mcp_read": True,
    },
    "inspect_rows": {
        "skills": {"reporting", "analysis", "excel_workflow"},
        "scenario_params": ("scenario_id",),
        "report_context": ("filters", "period_start", "period_end"),
        "mcp_read": True,
    },
    "list_variants": {
        "skills": {"reporting", "analysis", "planning"},
        "scenario_params": ("scenario_id",),
        "mcp_read": True,
    },

    # --- utility read tools (base / always-visible; exposed over MCP) ---
    # These three carry explicit metadata so the catalogue is the full
    # record (base tool, access="read"), and the `mcp_read` flag is what
    # publishes them to the external transport.
    # No `skills` key keeps them base tools (always visible to the agent),
    # matching their prior default behaviour.
    "list_scenarios": {
        "mcp_read": True,
    },
    "list_kpis": {
        "mcp_read": True,
    },
    "search_hierarchy": {
        "mcp_read": True,
    },
    "list_dimensions": {
        "mcp_read": True,
    },
    "resolve_to_cc_list": {},
    "reload_catalogue": {
        "skills": {"reporting", "analysis"},
    },

    # --- ingestion read tools (analyst+, no orchestrator triggers) ---
    # Exposed over /mcp so finance users can self-serve data-freshness
    # questions ("when did April land?") from any client.
    "list_load_history": {
        "skills": {"reporting", "analysis", "planning"},
        "mcp_read": True,
    },
    "get_load_status": {
        "skills": {"reporting", "analysis", "planning"},
        "mcp_read": True,
    },
    "list_bindings": {
        "skills": {"reporting", "analysis", "planning"},
        "mcp_read": True,
    },
    "get_binding": {
        "skills": {"reporting", "analysis", "planning"},
        "mcp_read": True,
    },
    "reload_integrations": {
        "skills": {"reporting", "analysis"},
        "access": "admin",
    },
}
# NOTE: this is the OPEN tool set only. Commercial tools (write/plan, reports,
# routines, workstreams, files/sandbox, charts/analysis, excel, conversation,
# skill/onboarding, plan-commit reads) register their catalogue entries via
# `register_tool_loader` from the Précis tool package.


# ---------------------------------------------------------------------------
# Tool loading and wrapping
# ---------------------------------------------------------------------------

# Parameters injected at call-time (hidden from LLM-facing schema).
_INJECTED_PARAMS: frozenset[str] = frozenset({"user_id", "_scope"})


def derive_llm_facing_signature(
    func: Callable,
) -> tuple[inspect.Signature, dict[str, Any]]:
    """Compute the signature and annotations the LLM sees for a tool function.

    Strips the call-time-injected params (``_INJECTED_PARAMS``) from the
    function's signature and annotation map. The result is the shared input
    used by both LLM-facing surfaces: the LangGraph wrapper feeds it into
    ``StructuredTool.from_function`` so LangChain derives the Pydantic
    ``args_schema``; the MCP ``tools/list`` handler will derive a JSON
    Schema from the same data.

    Pure: no side effects, no globals beyond ``_INJECTED_PARAMS``. Both
    surfaces must consume this function — never duplicate the stripping
    rule, or the two channels will drift.
    """
    sig = inspect.signature(func)
    injected = _INJECTED_PARAMS & set(sig.parameters)
    new_params = [p for name, p in sig.parameters.items() if name not in injected]
    new_sig = sig.replace(parameters=new_params)
    new_annotations = {
        k: v for k, v in getattr(func, "__annotations__", {}).items()
        if k not in injected
    }
    return new_sig, new_annotations


def _make_agent_wrapper(descriptor: ToolDescriptor) -> Callable:
    """Wrap a tool function for LLM binding: hide injected params, inject at call.

    Schema construction is delegated to :func:`derive_llm_facing_signature`
    so the LangGraph and MCP channels share one source of truth for the
    LLM-visible shape of every tool.

    Unknown-kwarg rejection is handled by Pydantic's ``extra='forbid'`` on the
    StructuredTool args_schema (set in ``build_agent_tools``); we don't need a
    redundant guard here because LangChain validates against the schema before
    invoking the wrapper.
    """
    func = descriptor.func
    new_sig, new_annotations = derive_llm_facing_signature(func)

    # Determine which injected params this function actually consumes so the
    # call-time inject branch only does work for params that exist on func.
    full_params = set(inspect.signature(func).parameters)
    needs_user_id = "user_id" in full_params
    needs_scope = "_scope" in full_params
    audited = descriptor.access in ("write", "plan_manager", "admin")

    if not (needs_user_id or needs_scope or audited):
        def wrapper(**kwargs: Any) -> Any:
            return func(**kwargs)
    else:
        def wrapper(**kwargs: Any) -> Any:
            if needs_user_id:
                kwargs["user_id"] = get_auth_context().user_id
            if needs_scope:
                kwargs["_scope"] = get_call_scope()
            if not audited:
                return func(**kwargs)
            try:
                result = func(**kwargs)
            except Exception as exc:
                _audit_tool_outcome(descriptor, kwargs, "exception", str(exc))
                raise
            err = result.get("error") if isinstance(result, dict) else None
            _audit_tool_outcome(
                descriptor, kwargs, "error" if err else "ok",
                str(err) if err else None,
            )
            return result

    wrapper.__name__ = func.__name__
    wrapper.__qualname__ = func.__qualname__
    wrapper.__doc__ = func.__doc__
    wrapper.__module__ = func.__module__
    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    wrapper.__annotations__ = new_annotations

    return wrapper


class _ToolRegistry:
    """Collects tool functions from register_*_tools() factories.

    Duck-types FastMCP's ``@mcp.tool()`` decorator so the same
    ``register_*_tools`` functions work for both the MCP server
    and the agent graph.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


# ---------------------------------------------------------------------------
# Commercial tool-set extension point
# ---------------------------------------------------------------------------
# The open package ships only the read-only tool set. The Précis platform
# registers its additional tools + catalogue metadata at startup via
# `register_tool_loader` (called from the Précis tool package). An open
# deployment registers none, so `build_descriptors` returns the open set and
# this module imports no Précis tool module.

_EXTRA_TOOL_LOADERS: list[Callable[[Any, Any], None]] = []
_EXTRA_CATALOGUE: dict[str, dict[str, Any]] = {}


def register_tool_loader(
    loader: Callable[[Any, Any], None],
    catalogue_entries: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Register a Précis tool loader and its catalogue metadata.

    ``loader(reg, catalogue_ref)`` registers a batch of tools onto the
    registry (the same ``register_*_tools`` shape the open loaders use).
    ``catalogue_entries`` is merged into the catalogue used by
    ``build_descriptors``. Idempotent per loader identity so repeated test
    installs don't duplicate.
    """
    if loader not in _EXTRA_TOOL_LOADERS:
        _EXTRA_TOOL_LOADERS.append(loader)
    if catalogue_entries:
        _EXTRA_CATALOGUE.update(catalogue_entries)


def tool_catalogue() -> dict[str, dict[str, Any]]:
    """The full catalogue: open ``TOOL_CATALOGUE`` plus registered Précis
    entries. Consumers that need the complete view (descriptor build,
    catalogue-integrity checks) use this, not ``TOOL_CATALOGUE`` directly."""
    return {**TOOL_CATALOGUE, **_EXTRA_CATALOGUE}


def _load_open_tools(reg: Any, catalogue_ref: Any) -> None:
    """Register the open (read-only) tool set: metric/statement/inspect,
    list/search, validation, and ingestion reads."""
    from precis_mcp.tools.read_tools import register_read_tools
    from precis_mcp.tools.ingestion_tools import register_ingestion_tools
    from precis_mcp.ingestion.wiring import (
        IntegrationRegistryRef,
        _default_integrations_root,
    )

    register_read_tools(reg, catalogue_ref)

    # Ingestion tools (list_load_history, get_load_status, list_bindings,
    # get_binding, reload_integrations). The registry ref here is constructed
    # locally; the API process holds a separate ref for federated reads and
    # admin reload — note that those two refs do not share state, so an admin
    # reload via /api/admin/reload_integrations does not refresh the agent's
    # view of the registry. Acceptable today because the agent tools only
    # inspect static metadata (bindings, datasets); a reload the agent needs
    # to see can wait for the next API-process restart.
    _integration_ref = IntegrationRegistryRef(_default_integrations_root())
    register_ingestion_tools(reg, _integration_ref)


def _load_all_tools(catalogue_ref) -> dict[str, Any]:
    """Load the open tool set plus any registered Précis loaders."""
    reg = _ToolRegistry()
    _load_open_tools(reg, catalogue_ref)
    for loader in _EXTRA_TOOL_LOADERS:
        loader(reg, catalogue_ref)
    return reg.tools


def build_descriptors(catalogue_ref) -> dict[str, ToolDescriptor]:
    """Load all tools and build the name→ToolDescriptor index.

    Shared by both transports. The LangGraph path wraps each descriptor as a
    LangChain ``StructuredTool`` (in the Précis agent's tool-registry
    layer); the MCP path derives a JSON Schema via ``build_tool_json_schema``.
    Catalogue typos are caught here at load time, not at runtime. The catalogue
    is the merged open + registered-Précis view (``tool_catalogue()``).
    """
    raw_funcs = _load_all_tools(catalogue_ref)
    catalogue = tool_catalogue()

    # Forward-drift guard: a catalogue entry with no registered tool function is
    # silently absent from the bound tool set (build only iterates raw_funcs), so
    # fail loud at load time — the catalogue and the registration must not drift
    # apart. The reverse drift (registered but uncatalogued) is caught in the
    # loop below.
    unregistered = set(catalogue) - set(raw_funcs)
    if unregistered:
        raise ValueError(
            f"Catalogue entries with no registered tool function: "
            f"{sorted(unregistered)}. Every TOOL_CATALOGUE / COMMERCIAL_CATALOGUE "
            "entry must have a factory that registers its function."
        )

    descriptors: dict[str, ToolDescriptor] = {}
    for name, func in raw_funcs.items():
        meta = catalogue.get(name)
        if meta is None:
            # Fail closed: an uncatalogued tool would silently inherit
            # access="read" and bypass the scenario-scope gate.
            raise ValueError(
                f"Tool '{name}' is loaded but has no catalogue entry. "
                "Every registered tool must appear in TOOL_CATALOGUE / "
                "COMMERCIAL_CATALOGUE (an empty dict is a valid entry)."
            )
        report_ctx = tuple(meta.get("report_context", ()))
        # Defensive: any catalogue typo is caught at startup, not at runtime.
        unknown_keys = set(report_ctx) - _VALID_REPORT_CONTEXT_KEYS
        if unknown_keys:
            raise ValueError(
                f"Tool '{name}' declares unknown report_context key(s): "
                f"{sorted(unknown_keys)}. Allowed: "
                f"{sorted(_VALID_REPORT_CONTEXT_KEYS)}"
            )
        descriptors[name] = ToolDescriptor(
            name=name,
            func=func,
            skills=frozenset(meta.get("skills", set())),
            access=meta.get("access", "read"),
            mcp_read=meta.get("mcp_read", False),
            scenario_params=tuple(meta.get("scenario_params", ())),
            default_scenario=meta.get("default_scenario", ""),
            report_context=report_ctx,
            dimension_map_args=dict(meta.get("dimension_map_args", {})),
        )
    return descriptors


def build_tool_json_schema(descriptor: ToolDescriptor) -> dict:
    """JSON Schema for a tool's LLM-facing arguments.

    Pydantic-native counterpart to LangChain's ``StructuredTool.args_schema``
    on the agent path: builds a model from the same LLM-facing signature
    (``derive_llm_facing_signature`` — injected params stripped) with
    ``extra='forbid'`` (→ ``additionalProperties: false``, matching the agent
    path's unknown-kwarg rejection), then emits its JSON Schema. The MCP
    ``tools/list`` handler consumes this; deriving the schema here, without
    LangChain, is what lets the external transport advertise tools without
    importing the agent runtime.
    """
    func = descriptor.func
    new_sig, new_annotations = derive_llm_facing_signature(func)

    fields: dict[str, Any] = {}
    for pname, param in new_sig.parameters.items():
        ann = new_annotations.get(pname, param.annotation)
        if ann is inspect.Parameter.empty:
            ann = Any
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (ann, default)

    model = create_model(  # type: ignore[call-overload]
        func.__name__,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model.model_json_schema()


# ---------------------------------------------------------------------------
# Report context default injection
# ---------------------------------------------------------------------------


def apply_report_context(
    tool_args: dict, context: dict, allowed: tuple[str, ...]
) -> dict:
    """Merge report context defaults into tool args.

    Only injects keys listed in ``allowed`` — declared per-tool via
    ``ToolDescriptor.report_context``. Tools that don't accept a given
    context key (e.g. ``export_plan_template`` ignores ``scenarios``)
    must omit it from their declaration; otherwise Pydantic's
    ``extra='forbid'`` will reject the call.

    Key semantics:
    - param absent OR None → inject context default
    - ``tool_args["filters"] == {}`` → explicit "no filters" (override)
    - ``tool_args["filters"] == {"x": "y"}`` → explicit filter (override)

    LLMs often send all tool parameters with ``None`` for fields they
    don't intend to override. We treat ``None`` as "not provided" so
    that context defaults are still injected. Only an explicit non-None
    value counts as an intentional override.
    """
    if not context or not allowed:
        return tool_args

    merged = dict(tool_args)
    allow = set(allowed)

    def _not_provided(key: str) -> bool:
        return key not in merged or merged[key] is None

    # filters: explicit {} is a meaningful override ("no filters").
    if "filters" in allow:
        if _not_provided("filters") and context.get("filters"):
            merged["filters"] = dict(context["filters"])
        elif merged.get("filters") is None:
            merged.pop("filters", None)

    # scenarios: copy defensively so callers can't mutate context.
    if "scenarios" in allow:
        if _not_provided("scenarios") and context.get("scenarios"):
            merged["scenarios"] = list(context["scenarios"])

    # scale / decimals: 0 is a valid value, so check `is not None` not truthiness.
    for key in ("scale", "decimals"):
        if key in allow and _not_provided(key) and context.get(key) is not None:
            merged[key] = context[key]

    # Plain string defaults — empty string is treated as "unset".
    for key in ("statement", "period_start", "period_end"):
        if key in allow and _not_provided(key) and context.get(key):
            merged[key] = context[key]

    return merged


# ---------------------------------------------------------------------------
# Pre-execution pipeline
# ---------------------------------------------------------------------------



def _coerce_list(val) -> list | None:
    """Coerce a JSON-stringified list back to a real list.

    LLMs sometimes double-encode list params as '["a","b"]' strings.
    """
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return [val]
    return [val]


# Params that should be lists — coerced before validation/execution
_LIST_PARAMS = {"columns", "dimensions", "metrics", "scenarios"}

# Within _LIST_PARAMS, these must be list[str] (not list[dict]). LLMs
# occasionally wrap entries — e.g. metrics=[{"metric": "gl_amount"}] —
# which would survive _coerce_list (it only checks list-ness) and crash
# downstream when the entry hits a set lookup. Auto-unwrap the canonical
# {"<param-singular>": "<key>"} shape; reject other shapes with a clean
# error so the agent corrects on retry rather than silently failing.
_LIST_OF_STR_PARAMS: dict[str, str] = {
    "columns": "column",
    "dimensions": "dimension",
    "metrics": "metric",
}

# Params that should be dicts — coerced before validation/execution
_DICT_PARAMS = {"filters", "chart_spec", "commit_scope", "discard_scope"}


def _normalise_list_of_str(
    param: str, val: list, unwrap_key: str,
) -> tuple[list[str] | None, str | None]:
    """Ensure *val* is a list of strings.

    Auto-unwraps the canonical ``[{"<unwrap_key>": "X"}]`` shape (the
    LLM mirroring the ``scenarios=[{"scenario": "X"}]`` pattern onto
    list-of-string params). Returns ``(cleaned, None)`` on success, or
    ``(None, err)`` with a corrective example if the shape can't be
    salvaged. Crashing in pre-flight on a dict-in-set lookup was the
    failure mode this guards against.
    """
    cleaned: list[str] = []
    for i, entry in enumerate(val):
        if isinstance(entry, str):
            cleaned.append(entry)
        elif isinstance(entry, dict) and unwrap_key in entry \
                and isinstance(entry[unwrap_key], str):
            cleaned.append(entry[unwrap_key])
        else:
            example_good = f"['{unwrap_key}_key']"
            example_bad = f"[{{'{unwrap_key}': '{unwrap_key}_key'}}]"
            return None, (
                f"{param!r} must be a list of strings, got "
                f"{type(entry).__name__} at index {i}: {entry!r}. "
                f"Pass {example_good}, not {example_bad}."
            )
    return cleaned, None


def _coerce_dict(val) -> dict | None:
    """Coerce a JSON-stringified dict back to a real dict.

    LLMs sometimes double-encode dict params as '{\"key\": \"val\"}' strings.
    """
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return val  # return as-is if we can't parse


def _extract_scenario_ids(
    args: dict, scenario_params: tuple[str, ...],
) -> list[str]:
    """Extract scenario IDs from tool args using the descriptor's param names.

    Handles both string params (``scenario_id``) and list-of-dict params
    (``scenarios`` with ``[{"scenario": "..."}]``).  Returns a flat deduplicated
    list of scenario refs with modifiers retained — the permission expansion
    must see modifiers because a ``fork`` target executes as its own scenario.
    """
    seen: set[str] = set()
    result: list[str] = []
    for param_name in scenario_params:
        val = args.get(param_name)
        if not val:
            continue
        if isinstance(val, str):
            if val not in seen:
                seen.add(val)
                result.append(val)
        elif isinstance(val, list):
            for entry in val:
                s = entry.get("scenario") if isinstance(entry, dict) else entry
                if s:
                    ref = str(s)
                    if ref not in seen:
                        seen.add(ref)
                        result.append(ref)
    return result


def _expand_scenario_ref_for_permissions(ref: str, catalogue: Any | None) -> set[str]:
    """Expand a scenario ref to the DB scenario IDs the caller must hold.

    Includes the base scenario's registry dependencies plus any ``fork``
    modifier target — an explicit ``fork=<id>`` target, or each variant's
    parent for a bare ``fork`` — because the engine substitutes the fork
    target as the executed scenario *after* this gate runs. Unresolvable
    refs are kept literally so the permission lookup fails closed.
    """
    del catalogue

    from precis_mcp.engine.resolver import parse_scenario_modifiers

    try:
        base, modifiers = parse_scenario_modifiers(ref)
    except Exception:
        # Unknown modifier key — gate on the base; the resolver rejects the
        # call with its own error before anything executes.
        base, modifiers = ref.split("&", 1)[0], {}

    registry = None
    base_ids: set[str] = set()
    try:
        from precis_mcp.db import get_clickhouse_client
        from precis_mcp.engine.scenario_registry import get_scenario_registry

        registry = get_scenario_registry(get_clickhouse_client)
        base_ids = registry.expand_dependencies(base)
    except Exception:
        logger.info(
            "Scenario registry expansion failed for %r",
            base,
            exc_info=True,
        )
    if not base_ids:
        # Not a registry key — keep the literal so direct scenario_ids still
        # resolve and anything else fails the permission lookup with a clear
        # "No access" error.
        base_ids = {base}

    if "fork" not in modifiers:
        return base_ids

    fork_ids: set[str] = set()
    target = modifiers["fork"]
    if target:
        if registry is not None:
            try:
                fork_ids = registry.expand_dependencies(target)
            except Exception:
                fork_ids = set()
        if not fork_ids:
            fork_ids = {target}
    elif registry is not None:
        # Bare fork: the engine substitutes each variant's parent (from the
        # same semantic.scenarios source) at execution time.
        parent_by_id = {
            s.scenario_id: s.variant_of for s in registry.list_real_scenarios()
        }
        fork_ids = {
            parent for sid in base_ids if (parent := parent_by_id.get(sid))
        }
    else:
        # Registry unavailable — the parent cannot be resolved, so the
        # substitution target cannot be authorized. Deny via a ref that no
        # permission map contains.
        fork_ids = {f"{base}&fork"}

    return base_ids | fork_ids


def process_tool_call(
    descriptor: ToolDescriptor,
    tool_args: dict,
    report_context: dict,
    catalogue=None,
) -> tuple[dict, str | None]:
    """Run the pre-execution pipeline for a single tool call.

    Steps:
    1. Coerce list/dict params
    2. Report context injection
    3. Permission gate (role + scenario + scope)
    4. Filter validation against catalogue
    5. Dimension validation against catalogue

    The gate runs before the catalogue/ClickHouse-backed validations so an
    unauthorized caller cannot probe dimension-member existence (or consume
    DB work) through validation error messages.

    Returns:
        (modified_args, error_or_none)
    """
    args = dict(tool_args)

    # Never inherit a previous call's scope: transports clear the contextvar
    # in their own finally blocks, but the gate must not depend on that.
    clear_call_scope()

    # 1. Coerce list/dict params (LLMs sometimes double-encode as JSON strings)
    for param in _LIST_PARAMS:
        if param in args:
            args[param] = _coerce_list(args[param])
    for param in _DICT_PARAMS:
        if param in args:
            args[param] = _coerce_dict(args[param])

    # 1b. Shape-check list[str] params — auto-unwrap [{"metric": "X"}]
    # shape, reject anything else with a corrective error.
    for param, unwrap_key in _LIST_OF_STR_PARAMS.items():
        val = args.get(param)
        if val is None or not isinstance(val, list):
            continue
        cleaned, err = _normalise_list_of_str(param, val, unwrap_key)
        if err is not None:
            return args, err
        args[param] = cleaned

    # 2. Report context injection (per-tool allow-list)
    if descriptor.report_context:
        args = apply_report_context(args, report_context, descriptor.report_context)

    # 2b. Default-scenario injection — must precede the gate.
    # A scenario-scoped read tool that defaults its scenario *in the tool body*
    # would otherwise reach the gate with no scenario: the read-class
    # pass-through sets no scope and the tool queries its default unscoped.
    # Injecting the declared default here routes every such call through the
    # single scoped path. Session/report-context defaults (step 2) take
    # priority; this is the fallback when neither the caller nor the context
    # named one.
    if descriptor.default_scenario and descriptor.scenario_params:
        if not _extract_scenario_ids(args, descriptor.scenario_params):
            args = {
                **args,
                descriptor.scenario_params[0]: [
                    {"scenario": descriptor.default_scenario}
                ],
            }

    # 3. Permission gate (role + scenario + scope)
    gate_error = _permission_gate(descriptor, args, catalogue)
    if gate_error is not None:
        _audit_gate_denial(descriptor, args, gate_error)
        return args, gate_error

    # 4. Dimension-map validation (filters, commit_scope, ...)
    if descriptor.dimension_map_args and catalogue is not None:
        from precis_mcp.engine.context_validation import validate_dimension_map

        for arg_name, target_key in descriptor.dimension_map_args.items():
            m = args.get(arg_name)
            if not m:
                continue
            result = validate_dimension_map(
                m, target_key, descriptor.name, args, catalogue,
            )
            args = {**args, arg_name: result.cleaned}
            if result.errors:
                return args, (
                    f"{arg_name} validation failed:\n" + "\n".join(result.errors)
                )
            for w in result.warnings:
                logger.info("%s validation: %s", arg_name, w)

    # 5. Dimension validation
    if args.get("dimensions") and catalogue is not None:
        from precis_mcp.engine.context_validation import validate_dimensions_for_report

        dv = validate_dimensions_for_report(
            args["dimensions"], descriptor.name, args, catalogue,
        )
        args = {**args, "dimensions": dv.cleaned_dimensions}
        if dv.errors:
            return args, "Dimension validation failed:\n" + "\n".join(dv.errors)
        for w in dv.warnings:
            logger.info("Dimension validation: %s", w)

    return args, None


def _permission_gate(
    descriptor: ToolDescriptor,
    args: dict,
    catalogue=None,
) -> str | None:
    """Role + scenario + scope gate. Returns the denial message or None.

    On success for scenario-scoped tools, publishes the per-scenario
    ``ScopeSpec`` map on the ``_call_scope`` contextvar.
    """
    access = descriptor.access
    auth_ctx = get_auth_context()
    permissions = auth_ctx.permissions

    # Admin tools — requires is_admin flag
    if access == "admin":
        if not permissions.is_admin:
            return f"Admin access required for '{descriptor.name}'"
        return None

    # General tools — no scope/scenario check
    if access == "general":
        return None

    # Scenario-scoped tools (read / write / plan_manager)
    scenario_refs = _extract_scenario_ids(args, descriptor.scenario_params)

    if not scenario_refs:
        # Write-class tools must resolve to a scenario; absence is denied
        # (otherwise an LLM-omitted scenario_id silently bypasses scope).
        if access in ("write", "plan_manager"):
            return (
                f"Tool '{descriptor.name}' requires a scenario_id "
                "but none was supplied"
            )
        # Read-class and general tools with no scenario specified pass through
        # (e.g. list_kpis, search_hierarchy, reload_catalogue).
        return None

    # Normalise registry aliases/generated keys to underlying DB scenario_ids
    # before the permission check.
    from precis_mcp.auth import ScopeSpec

    per_scenario_scope: dict[str, ScopeSpec | None] = {}

    for ref in scenario_refs:
        db_ids = _expand_scenario_ref_for_permissions(ref, catalogue)

        for sid in db_ids:
            scenario_perms = permissions.scenarios.get(sid)
            if scenario_perms is None:
                return f"No access to scenario '{sid}'"

            if access not in scenario_perms.tool_scopes:
                return (
                    f"Role '{scenario_perms.effective_role}' cannot use "
                    f"'{descriptor.name}' (requires {access}) on scenario '{sid}'"
                )

            per_scenario_scope[sid] = scenario_perms.tool_scopes[access]

    # Store per-scenario scope in contextvar for wrapper injection
    if any(v is not None for v in per_scenario_scope.values()):
        set_call_scope(per_scenario_scope)

    return None


def _audit_gate_denial(
    descriptor: ToolDescriptor,
    args: dict,
    error: str,
) -> None:
    """Append a write/admin-class gate denial to ``security_audit_log``.

    Allowed write-class calls are audited with their outcome at invocation
    time — by ``_make_agent_wrapper`` on the agent path and by the external
    transport's ``mcp_tool_call`` event on the MCP path — so the gate only
    records denials. Audit failure is logged and never blocks the response.
    """
    if descriptor.access not in ("write", "plan_manager", "admin"):
        return
    try:
        from precis_mcp import db

        details = {
            "tool_name": descriptor.name,
            "access": descriptor.access,
            "outcome": "denied",
            "error": error[:500],
            "args_preview": json.dumps(args, default=str)[:2000],
        }
        db.write_security_audit(
            "tool_gate_denied", get_auth_context().user_id, details=details,
        )
    except Exception:
        logger.exception(
            "gate-denial audit write failed (tool=%s) — continuing",
            descriptor.name,
        )


def _audit_tool_outcome(
    descriptor: ToolDescriptor,
    kwargs: dict,
    outcome: str,
    error: str | None,
) -> None:
    """Append a write/admin-class tool invocation + outcome (agent path).

    The MCP transport writes its own ``mcp_tool_call`` rows; this covers the
    LangGraph agent path, where ``_make_agent_wrapper`` is the invocation
    chokepoint. Audit failure is logged and never blocks the tool result.
    """
    try:
        from precis_mcp import db

        preview = {k: v for k, v in kwargs.items() if k != "_scope"}
        details = {
            "tool_name": descriptor.name,
            "access": descriptor.access,
            "outcome": outcome,
            "args_preview": json.dumps(preview, default=str)[:2000],
            "transport": "agent",
        }
        if error:
            details["error"] = error[:500]
        db.write_security_audit(
            "agent_tool_call", get_auth_context().user_id, details=details,
        )
    except Exception:
        logger.exception(
            "tool-outcome audit write failed (tool=%s) — continuing",
            descriptor.name,
        )
