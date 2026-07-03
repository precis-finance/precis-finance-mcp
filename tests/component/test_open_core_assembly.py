# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-core standalone assembly.

The import-linter proves there are no open→Précis *imports*. This proves the
open dispatch core + open tool set actually *assemble and serve a query* with no
Précis tools registered — i.e. without the Précis tool loader. It
catches runtime coupling the linter cannot see: an open tool that reads state,
config, or a catalogue entry only the Précis platform provides.

The session conftest installs the Précis loader (mirroring Précis startup), so
each test here strips it back out via the ``open_only`` fixture and asserts the
open deployment stands alone.
"""

from __future__ import annotations

import importlib.util
from unittest.mock import patch

import pytest

from tests.factories.auth import (
    make_auth_headers,
    make_permissions,
    make_scenario_permissions,
)


# The open tool set: read tools + ingestion reads + validation.
# Anything beyond this in build_descriptors() under open_only is a leak.
OPEN_TOOLS = {
    "run_statement", "run_metric", "list_scenarios", "list_kpis",
    "list_inspection_sources", "get_inspection_schema", "inspect_rows",
    "search_hierarchy", "list_dimensions", "list_variants",
    "resolve_to_cc_list", "reload_catalogue", "list_load_history",
    "get_load_status", "list_bindings", "get_binding", "reload_integrations",
}

# A sample of Précis platform tools that must be ABSENT from the open assembly.
# run_validation_check is a Précis tool — plan-entry validation only exists under
# the write path. web_fetch is a Précis tool — it depends on the
# Anthropic SDK + an API key, outside the open package's zero-LLM read surface.
COMMERCIAL_ABSENT = {
    "save_plan_entries", "commit_plan", "create_report", "eval_chart_transform",
    "emit_analysis", "list_commits", "get_pending_changes", "execute_python",
    "create_routine", "create_workstream", "activate_skill", "list_users",
    "run_validation_check", "web_fetch",
}


class _MockRef:
    def __init__(self, catalogue):
        self.current = catalogue


@pytest.fixture
def open_only(monkeypatch):
    """Assemble as an open-only deployment: strip the Précis tool loader +
    catalogue the conftest installed, and reset the MCP transport's lazy
    descriptor cache so it rebuilds open-only. monkeypatch restores all of it
    (including the cache) after the test, so the Précis set is back for the
    next test."""
    import precis_mcp.dispatch as dispatch
    import precis_mcp.mcp_external.server as mcp_transport

    monkeypatch.setattr(dispatch, "_EXTRA_TOOL_LOADERS", [])
    monkeypatch.setattr(dispatch, "_EXTRA_CATALOGUE", {})
    monkeypatch.setattr(mcp_transport, "_descriptors", None)
    monkeypatch.setattr(mcp_transport, "_schemas_by_name", None)
    monkeypatch.setattr(mcp_transport, "_wrappers", None)
    yield


def test_open_dispatch_assembles_exactly_the_open_tool_set(open_only, catalogue):
    """With no Précis loader registered, build_descriptors yields exactly the
    open read-only tool set — no write/plan/report/chart/analysis tools."""
    from precis_mcp.dispatch import build_descriptors

    descriptors = build_descriptors(_MockRef(catalogue))
    assert set(descriptors) == OPEN_TOOLS
    assert not (set(descriptors) & COMMERCIAL_ABSENT)
    # The open package is read-only: no write-class tools assemble.
    assert all(
        d.access not in {"write", "plan_manager"} for d in descriptors.values()
    )


@pytest.mark.skipif(
    importlib.util.find_spec("precis") is None,
    reason="Précis-monorepo-only: web_fetch is a commercial base tool, absent "
    "from the open export tree — nothing binds it without the Précis loader",
)
def test_commercial_assembly_binds_web_fetch(catalogue):
    """Regression: web_fetch is a commercial base tool and must be bound in the
    full (Précis-loader-installed) assembly.

    It was catalogued in COMMERCIAL_CATALOGUE but its factory was never called
    from the open-core split (af6abd5) until it was wired into
    register_commercial_tools — so it silently never bound, and the agent
    reported it unavailable. build_descriptors' forward-drift guard now fails
    loud if any catalogue entry lacks a registered function.
    """
    from precis_mcp.dispatch import build_descriptors

    descriptors = build_descriptors(_MockRef(catalogue))
    assert "web_fetch" in descriptors
    assert descriptors["web_fetch"].access == "read"
    assert not descriptors["web_fetch"].skills  # base tool — always visible


@pytest.mark.skipif(
    importlib.util.find_spec("precis") is None,
    reason="Précis-monorepo-only: drives the open /mcp transport through the "
    "Précis agent app (the `client` fixture) to prove the split in situ",
)
def test_open_core_serves_a_read_query_over_mcp(open_only, client, ch_client):
    """End-to-end: the open /mcp transport advertises only open tools and
    executes a read tool (list_kpis) with no Précis tools registered."""
    perms = make_permissions(
        user_id="alice", scenarios={"BUD-2026": make_scenario_permissions()},
    )
    with patch(
        "precis_mcp.mcp_external.server.load_permissions", return_value=perms,
    ), patch(
        "precis_mcp.tools.read_tools.get_clickhouse_client", return_value=ch_client,
    ):
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=make_auth_headers("alice"),
        )
        called = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "list_kpis", "arguments": {}}},
            headers=make_auth_headers("alice"),
        )

    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert "run_statement" in names              # an open tool is advertised
    assert "eval_chart_transform" not in names   # a Précis mcp_read tool is absent
    result = called.json()["result"]
    assert result["isError"] is False
    assert "structuredContent" in result


def test_app_open_entrypoint_boots_standalone(open_only, ch_client):
    """The open ASGI entrypoint (`precis_mcp.app_open`) — not agui — boots and
    serves the open surface, mounting none of the Précis routers. This is
    the independence proof: the open package serves reads over its
    own app, with no Précis application present."""
    from fastapi.testclient import TestClient

    from precis_mcp.app_open import app

    # The open app mounts only the open surface — no Précis routers.
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/mcp", "/health", "/.well-known/oauth-protected-resource"} <= paths
    assert not any(p and p.startswith("/api/") for p in paths)

    perms = make_permissions(
        user_id="alice", scenarios={"BUD-2026": make_scenario_permissions()},
    )
    with TestClient(app) as tc, patch(
        "precis_mcp.mcp_external.server.load_permissions", return_value=perms,
    ), patch(
        "precis_mcp.tools.read_tools.get_clickhouse_client", return_value=ch_client,
    ):
        assert tc.get("/health").json() == {"status": "ok"}
        assert tc.get("/.well-known/oauth-protected-resource").status_code == 200
        listed = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=make_auth_headers("alice"),
        )
        called = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "list_kpis", "arguments": {}}},
            headers=make_auth_headers("alice"),
        )

    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert "run_statement" in names
    assert "eval_chart_transform" not in names
    result = called.json()["result"]
    assert result["isError"] is False
    assert "structuredContent" in result


def test_open_instructions_exclude_commercial_surface(monkeypatch, catalogue):
    """The open MCP-connector instructions describe only the open read surface —
    no charts (`eval_chart_transform`), no Excel (`_excel`). The Précis
    product registers a full template override that re-introduces them
    (covered by test_mcp_external); here we assert the *open* default.

    The conftest installs the Précis tools (and thus the template override)
    session-wide, so we clear the override to exercise the open path."""
    import precis_mcp.mcp_external.instructions as instr

    monkeypatch.setattr(instr, "_TEMPLATE_OVERRIDE", None)
    text = instr.build_mcp_instructions(catalogue, scenario_registry=None)

    assert "eval_chart_transform" not in text
    assert "_excel" not in text
    assert "## Charts" not in text
    # The open read surface is still described.
    assert "run_statement_data" in text and "inspect_rows" in text
