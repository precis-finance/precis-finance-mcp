# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-package tool assembly, orientation and HTTP transport contracts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.factories.auth import (
    make_auth_headers,
    make_permissions,
    make_scenario_permissions,
)


# The open tool set: finance reads, datasets/charts and artifact tools.
# Anything beyond this in build_descriptors() under open_only is a leak.
OPEN_TOOLS = {
    "run_statement", "run_metric", "list_scenarios", "list_kpis",
    "list_inspection_sources", "get_inspection_schema", "inspect_rows",
    "search_hierarchy", "list_dimensions", "list_variants",
    "resolve_to_cc_list", "reload_catalogue", "list_load_history",
    "get_load_status", "list_bindings", "get_binding", "reload_integrations",
    "eval_chart_transform", "export_dataset", "begin_file_upload", "complete_file_upload", "get_file_download", "import_chatgpt_file",
}


class _MockRef:
    def __init__(self, catalogue):
        self.current = catalogue


def test_open_dispatch_assembles_exactly_the_open_tool_set(open_only, catalogue):
    """Without extension loaders, descriptor assembly matches the public tool set."""
    from precis_mcp.dispatch import build_descriptors

    descriptors = build_descriptors(_MockRef(catalogue))
    assert set(descriptors) == OPEN_TOOLS
    # The open package is read-only: no write-class tools assemble.
    assert all(
        d.access not in {"write", "plan_manager"} for d in descriptors.values()
    )


@pytest.mark.parametrize("backend", ["clickhouse", "databricks"])
def test_customer_managed_assembly_omits_ingestion_on_either_backend(open_only, catalogue, monkeypatch, backend):
    from precis_mcp.dispatch import build_descriptors
    from precis_mcp.tools.ingestion_tools import INGESTION_TOOL_NAMES
    monkeypatch.setenv("PRECIS_ANALYTICAL_BACKEND", backend)
    monkeypatch.setenv("PRECIS_INGESTION_MODE", "customer_managed")
    monkeypatch.setattr("precis_mcp.db.get_clickhouse_client", lambda: pytest.fail("opened CH during assembly"))
    descriptors = build_descriptors(_MockRef(catalogue), include_commercial=False)
    assert not set(descriptors) & INGESTION_TOOL_NAMES
    assert {"run_statement", "run_metric", "search_hierarchy", "inspect_rows"} <= set(descriptors)
    assert set(descriptors) == OPEN_TOOLS - set(INGESTION_TOOL_NAMES)


def test_remote_assembly_ignores_product_loader(catalogue, monkeypatch):
    from precis_mcp import dispatch
    called = []
    monkeypatch.setattr(dispatch, "_EXTRA_TOOL_LOADERS", [lambda *_: called.append(True)])
    descriptors = dispatch.build_descriptors(_MockRef(catalogue), include_commercial=False)
    assert called == []
    assert set(descriptors) == OPEN_TOOLS


def test_app_open_entrypoint_boots_standalone(open_only, ch_client):
    """The MCP application advertises and executes its own public read surface."""
    from fastapi.testclient import TestClient

    from precis_mcp.app_open import app

    # The open app mounts only the open surface — no Précis routers.
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/mcp", "/health", "/.well-known/oauth-protected-resource"} <= paths
    assert {p for p in paths if p and p.startswith("/api/")} == {"/api/artifacts/download", "/api/artifacts/upload"}

    perms = make_permissions(
        user_id="alice", scenarios={"BUD-2026": make_scenario_permissions()},
    )
    from tests.fakes.fake_redis import FakeRedis
    redis = FakeRedis()
    with patch("precis_mcp.artifacts.lifecycle.check_artifacts"), patch("precis_mcp.db.get_redis_client", return_value=redis), patch("precis_mcp.artifacts.lifecycle.get_redis_client", return_value=redis), TestClient(app) as tc, patch(
        "precis_mcp.request_auth.load_permissions", return_value=perms,
    ), patch(
        "precis_mcp.tools.read_tools.get_analytical_client", return_value=ch_client,
    ):
        assert tc.get("/health").json() == {"status": "ok"}
        discovery = tc.get("/.well-known/oauth-protected-resource")
        metadata = tc.get("/.well-known/oauth-authorization-server")
        assert discovery.status_code == metadata.status_code == 200
        assert discovery.json()["authorization_servers"] == [metadata.json()["issuer"]]
        assert metadata.json()["authorization_response_iss_parameter_supported"] is True
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
    assert "eval_chart_transform" in names
    result = called.json()["result"]
    assert result["isError"] is False
    assert "structuredContent" in result


def test_open_instructions_exclude_commercial_surface(monkeypatch, catalogue):
    """One core orientation describes datasets, charts and artifacts."""
    import precis_mcp.mcp_external.instructions as instr

    text = instr.build_mcp_instructions(catalogue, scenario_registry=None)

    assert "eval_chart_transform" in text
    assert "_excel" not in text
    assert "## Charts" in text
    assert "export_dataset" in text
    # The open read surface is still described.
    assert "run_statement_data" in text and "inspect_rows" in text
    assert "use `full_pnl` for a general P&L request" in text
    assert "Always give every scenario a user-facing `alias`" in text
