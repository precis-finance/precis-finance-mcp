# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the agent read-only ingestion tools — `list_load_history`,
`get_load_status`, `list_bindings`, `get_binding` — plus the existing
`reload_integrations` registration.

Uses the canonical `MockMCP` to capture decorator-registered tools and the
shared `FakePlatformDB` to substitute for Postgres on the load_history
queries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.wiring import IntegrationRegistryRef
from precis_mcp.tools.ingestion_tools import register_ingestion_tools
from tests.factories.ingestion import build_tree, make_binding, make_source
from tests.fakes.fake_platform_db import FakePlatformDB
from tests.fakes.mock_mcp import MockMCP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch):
    """Substitute `precis_mcp.db.{execute_platform,query_platform}` with the
    canonical in-memory fake. Tests then poke `fake_db.add_load_history_row`
    to seed canned rows the read tools consult."""
    db_fake = FakePlatformDB()

    monkeypatch.setattr(
        "precis_mcp.ingestion.load_history.db.query_platform",
        db_fake.query,
    )
    monkeypatch.setattr(
        "precis_mcp.ingestion.load_history.db.execute_platform",
        db_fake.execute,
    )
    return db_fake


@pytest.fixture
def registry_ref(tmp_path: Path) -> IntegrationRegistryRef:
    """Live IntegrationRegistry on disk so `list_bindings` / `get_binding`
    have something to read from. Bindings use the watch mode by default."""
    root = build_tree(
        tmp_path,
        sources=[make_source()],
        bindings=[make_binding()],
    )
    return IntegrationRegistryRef(root)


@pytest.fixture
def tools(registry_ref):
    """Build a MockMCP, register the ingestion tools, return the captured
    function dict for direct invocation."""
    mcp = MockMCP()
    register_ingestion_tools(mcp, registry_ref)
    return mcp.tools


# ---------------------------------------------------------------------------
# Tool registration surface
# ---------------------------------------------------------------------------


def test_all_five_tools_register(tools):
    assert set(tools.keys()) == {
        "reload_integrations",
        "list_load_history",
        "get_load_status",
        "list_bindings",
        "get_binding",
    }


# ---------------------------------------------------------------------------
# list_load_history
# ---------------------------------------------------------------------------


def _seed_load_history(fake_db: FakePlatformDB, rows: list[dict]) -> None:
    """Inject pre-populated load_history rows into the FakePlatformDB.

    FakePlatformDB doesn't natively know about load_history (the fake
    predates Phase 0), so we route the seed through INSERT statements that
    its `_route_execute` handler will land into a generic table dict —
    or, simpler, attach a custom rows attribute the query stub can read.
    Tests treat the seed as a one-shot setup helper.
    """
    # Bolt a load_history table onto the fake if it doesn't have one.
    if not hasattr(fake_db, "load_history"):
        fake_db.load_history = []  # type: ignore[attr-defined]

    fake_db.load_history.extend(rows)  # type: ignore[attr-defined]


def _patch_load_history_queries(monkeypatch, fake_db: FakePlatformDB) -> None:
    """Replace the load_history module's `db.query_platform` with a stub
    that returns the seeded rows after filtering by the SQL's WHERE clauses."""

    def stub(sql: str, params=None):
        params = list(params or ())
        rows = list(getattr(fake_db, "load_history", []))
        sql_lower = sql.lower()

        # `get_load_history_row` — single-row lookup by load_id.
        if "where load_id = " in sql_lower:
            load_id = params[0]
            return [r for r in rows if r.get("load_id") == load_id][:1]

        # `query_load_history` — apply each filter inferred from the SQL.
        param_iter = iter(params)
        if "binding_id = " in sql_lower:
            target = next(param_iter)
            rows = [r for r in rows if r.get("binding_id") == target]
        if "dataset_id = " in sql_lower:
            target = next(param_iter)
            rows = [r for r in rows if r.get("dataset_id") == target]
        if "period = " in sql_lower:
            target = next(param_iter)
            rows = [r for r in rows if r.get("period") == target]
        if "status = " in sql_lower:
            target = next(param_iter)
            rows = [r for r in rows if r.get("status") == target]

        # Final param is the LIMIT.
        limit = next(param_iter, None)
        rows = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)
        if isinstance(limit, int):
            rows = rows[:limit]
        return rows

    monkeypatch.setattr(
        "precis_mcp.ingestion.load_history.db.query_platform", stub
    )


def test_list_load_history_returns_seeded_rows(tools, fake_db, monkeypatch):
    _seed_load_history(
        fake_db,
        [
            {
                "load_id": "L1",
                "binding_id": "manual_drop__gl",
                "dataset_id": "gl",
                "period": "2026-04",
                "status": "success",
                "started_at": datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc),
            },
            {
                "load_id": "L2",
                "binding_id": "manual_drop__gl",
                "dataset_id": "gl",
                "period": "2026-03",
                "status": "failed_recon",
                "started_at": datetime(2026, 4, 14, 3, 0, tzinfo=timezone.utc),
            },
        ],
    )
    _patch_load_history_queries(monkeypatch, fake_db)

    out = tools["list_load_history"]()
    assert out["count"] == 2
    assert out["rows"][0]["load_id"] == "L1"  # most recent first
    # datetime serialised to ISO string.
    assert out["rows"][0]["started_at"].startswith("2026-04-15")


def test_list_load_history_filters_by_period(tools, fake_db, monkeypatch):
    _seed_load_history(
        fake_db,
        [
            {"load_id": "L1", "binding_id": "b", "dataset_id": "gl",
             "period": "2026-04", "status": "success",
             "started_at": datetime(2026, 4, 15, tzinfo=timezone.utc)},
            {"load_id": "L2", "binding_id": "b", "dataset_id": "gl",
             "period": "2026-03", "status": "success",
             "started_at": datetime(2026, 3, 15, tzinfo=timezone.utc)},
        ],
    )
    _patch_load_history_queries(monkeypatch, fake_db)

    out = tools["list_load_history"](period="2026-03")
    assert out["count"] == 1
    assert out["rows"][0]["load_id"] == "L2"


def test_list_load_history_filters_by_status(tools, fake_db, monkeypatch):
    _seed_load_history(
        fake_db,
        [
            {"load_id": "L1", "binding_id": "b", "dataset_id": "gl",
             "period": "2026-04", "status": "success",
             "started_at": datetime(2026, 4, 15, tzinfo=timezone.utc)},
            {"load_id": "L2", "binding_id": "b", "dataset_id": "gl",
             "period": "2026-04", "status": "failed_recon",
             "started_at": datetime(2026, 4, 14, tzinfo=timezone.utc)},
        ],
    )
    _patch_load_history_queries(monkeypatch, fake_db)

    out = tools["list_load_history"](status="failed_recon")
    assert out["count"] == 1
    assert out["rows"][0]["status"] == "failed_recon"


def test_list_load_history_limit_is_capped(tools, fake_db, monkeypatch):
    """Even a request for `limit=10_000` is hard-capped at 200."""
    _seed_load_history(fake_db, [])
    captured = {}

    def stub(sql: str, params=None):
        captured["params"] = list(params or ())
        return []

    monkeypatch.setattr(
        "precis_mcp.ingestion.load_history.db.query_platform", stub
    )

    tools["list_load_history"](limit=10_000)
    # Final SQL param is the LIMIT.
    assert captured["params"][-1] == 200


# ---------------------------------------------------------------------------
# get_load_status
# ---------------------------------------------------------------------------


def test_get_load_status_returns_full_row(tools, fake_db, monkeypatch):
    _seed_load_history(
        fake_db,
        [
            {
                "load_id": "L1",
                "binding_id": "manual_drop__gl",
                "status": "success",
                "started_at": datetime(2026, 4, 15, tzinfo=timezone.utc),
                "control_total_result": {"passed": True, "rules": []},
            }
        ],
    )
    _patch_load_history_queries(monkeypatch, fake_db)

    out = tools["get_load_status"]("L1")
    assert out["found"] is True
    assert out["row"]["load_id"] == "L1"
    assert out["row"]["control_total_result"] == {"passed": True, "rules": []}


def test_get_load_status_unknown_load_id(tools, fake_db, monkeypatch):
    _seed_load_history(fake_db, [])
    _patch_load_history_queries(monkeypatch, fake_db)

    out = tools["get_load_status"]("ghost-load-id")
    assert out["found"] is False
    assert out["load_id"] == "ghost-load-id"


# ---------------------------------------------------------------------------
# list_bindings
# ---------------------------------------------------------------------------


def test_list_bindings_returns_summary_shape(tools):
    out = tools["list_bindings"]()
    assert out["count"] == 1
    binding = out["bindings"][0]
    # Each entry carries the operationally meaningful fields.
    assert binding["id"] == "test_pg__fact_gl"
    assert binding["source"] == "test_pg"
    assert binding["target"] == "live.fact_gl"
    assert binding["kind"] == "period"
    assert binding["schedule"]["mode"] == "push"


def test_list_bindings_filters_by_source(tools):
    out = tools["list_bindings"](source_id="test_pg")
    assert out["count"] == 1
    out_no = tools["list_bindings"](source_id="ghost_source")
    assert out_no["count"] == 0


# ---------------------------------------------------------------------------
# get_binding
# ---------------------------------------------------------------------------


def test_get_binding_returns_full_config(tools):
    out = tools["get_binding"]("test_pg__fact_gl")
    assert out["found"] is True
    binding = out["binding"]
    assert binding["id"] == "test_pg__fact_gl"
    assert binding["source"] == "test_pg"
    assert binding["target"] == "live.fact_gl"
    # Full config — includes kind, scenario, extract block.
    assert binding["kind"] == "period"
    assert "extract" in binding


def test_get_binding_unknown_id(tools):
    out = tools["get_binding"]("ghost__binding")
    assert out["found"] is False
    assert out["binding_id"] == "ghost__binding"


# ---------------------------------------------------------------------------
# reload_integrations — sanity check (full behavioural coverage lives in
# `tests/component/test_integration_registry_reload.py`)
# ---------------------------------------------------------------------------


def test_reload_integrations_returns_summary(tools, registry_ref):
    """The reload tool calls registry_ref.reload() and returns its summary."""
    summary = tools["reload_integrations"]()
    assert "Reloaded" in summary
