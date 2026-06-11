# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/clickhouse_init.py`.

The provisioner composes the open runners (live → scenarios → semantic views)
with an extension seam for commercial steps that must land *before* the semantic
views. Tests cover the plan ordering (the load-bearing §4.1 invariant), the
`--scope open` skip, the register_step dedup, and one end-to-end wiring run
against a stub ClickHouse over a minimal instance tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from precis_mcp import clickhouse_init


@dataclass
class _Result:
    result_rows: list[tuple]


class _StubCH:
    def __init__(self, responses: dict[str, list[tuple]] | None = None) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple] = []
        self._responses = responses or {}  # substring -> rows

    def command(self, sql: str, *a, **k) -> None:
        self.commands.append(sql)

    def query(self, sql: str, parameters=None) -> _Result:
        for sub, rows in self._responses.items():
            if sub.lower() in sql.lower():
                return _Result(result_rows=list(rows))
        return _Result(result_rows=[])

    def insert(self, table, data, column_names=None, **k) -> None:
        self.inserts.append((table, data, column_names))


def _noop(_d, _ch):
    pass


# -- plan ordering -----------------------------------------------------------

def test_plan_open_scope_is_live_scenarios_semantic():
    names = [n for n, _ in clickhouse_init.plan("open", extension_steps=[])]
    assert names == ["live", "scenarios", "semantic_views"]


def test_plan_all_scope_runs_extension_before_semantic_views():
    ext = [("planning", _noop)]
    names = [n for n, _ in clickhouse_init.plan("all", extension_steps=ext)]
    # The load-bearing invariant: extension steps land AFTER scenarios and
    # BEFORE semantic views (a view may reference a table they create).
    assert names == ["live", "scenarios", "planning", "semantic_views"]
    assert names.index("planning") < names.index("semantic_views")


def test_plan_open_scope_skips_extension_steps():
    ext = [("planning", _noop)]
    names = [n for n, _ in clickhouse_init.plan("open", extension_steps=ext)]
    assert "planning" not in names


# -- register_step (touches module global — snapshot + restore) --------------

@pytest.fixture
def _clean_registry():
    saved = list(clickhouse_init._EXTENSION_STEPS)
    clickhouse_init._EXTENSION_STEPS.clear()
    yield
    clickhouse_init._EXTENSION_STEPS[:] = saved


def test_register_step_appends_then_dedups_by_name(_clean_registry):
    def f1(_d, _ch):
        pass

    def f2(_d, _ch):
        pass

    clickhouse_init.register_step("planning", f1)
    clickhouse_init.register_step("other", _noop)
    clickhouse_init.register_step("planning", f2)  # replace, not duplicate

    names = [n for n, _ in clickhouse_init._EXTENSION_STEPS]
    assert names == ["planning", "other"]
    fn = dict(clickhouse_init._EXTENSION_STEPS)["planning"]
    assert fn is f2


# -- provision wiring (stub CH over a minimal instance tree) -----------------

def _minimal_instance(root: Path) -> Path:
    (root / "live").mkdir(parents=True)
    (root / "live" / "fact_x.sql").write_text(
        "( a String ) ENGINE = MergeTree ORDER BY a"
    )
    (root / "scenarios.yml").write_text(
        "scenarios:\n  - {scenario_id: ACTUALS, alias: actuals, "
        "name: Actuals, kind: ACTUAL}\n"
    )
    (root / "semantic" / "dims").mkdir(parents=True)
    (root / "semantic" / "views").mkdir(parents=True)
    (root / "semantic" / "dims" / "dim_x.sql").write_text("SELECT 1")
    (root / "semantic" / "views" / "v_x.sql").write_text("SELECT 1")
    return root


def test_provision_open_runs_runners_in_load_bearing_order(tmp_path: Path):
    inst = _minimal_instance(tmp_path / "instance")
    ch = _StubCH()

    ran = clickhouse_init.provision(inst, ch, scope="open")
    assert ran == ["live", "scenarios", "semantic_views"]

    # live table created before the semantic view that could reference it.
    live_idx = next(
        i for i, c in enumerate(ch.commands) if "live.fact_x" in c
    )
    view_idx = next(
        i for i, c in enumerate(ch.commands) if "semantic.v_x" in c
    )
    assert live_idx < view_idx
    # scenarios table created + seeded between the two.
    assert any("semantic.scenarios" in c for c in ch.commands)
    assert ch.inserts and ch.inserts[0][0] == "semantic.scenarios"


# -- check (preflight) -------------------------------------------------------

from types import SimpleNamespace  # noqa: E402


def _fake_cat(domains: dict) -> SimpleNamespace:
    return SimpleNamespace(domains=domains)


def _ch_views_present(*views: str, scenario_count: int = 1) -> _StubCH:
    objs = [(v,) for v in views] + [("semantic.scenarios",)]
    return _StubCH(
        responses={
            "system.tables": objs,
            "count()": [(scenario_count,)],
        }
    )


def test_check_all_pass():
    cat = _fake_cat(
        {"gl": SimpleNamespace(source_view="semantic.v_gl", backend_kind="clickhouse")}
    )
    ch = _ch_views_present("semantic.v_gl", scenario_count=3)
    by = {r.name: r for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=lambda d: cat)}
    assert by["catalogue"].ok
    assert by["view:semantic.v_gl"].ok
    assert by["scenarios"].ok and "3 row" in by["scenarios"].detail


def test_check_reports_catalogue_failure_and_skips_view_checks():
    from precis_mcp.engine import CatalogueError

    def boom(_d):
        raise CatalogueError("duplicate metric 'revenue'")

    ch = _ch_views_present("semantic.v_gl")
    by = {r.name: r for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=boom)}
    assert not by["catalogue"].ok and "revenue" in by["catalogue"].detail
    # No view checks ran — the catalogue never loaded.
    assert not any(n.startswith("view:") for n in by)


def test_check_flags_missing_semantic_view():
    cat = _fake_cat(
        {"gl": SimpleNamespace(source_view="semantic.v_gl", backend_kind="clickhouse")}
    )
    ch = _ch_views_present()  # v_gl absent
    by = {r.name: r for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=lambda d: cat)}
    assert not by["view:semantic.v_gl"].ok


def test_check_flags_empty_scenarios():
    ch = _StubCH(responses={"system.tables": [], "count()": [(0,)]})
    by = {r.name: r for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=lambda d: _fake_cat({}))}
    assert not by["scenarios"].ok and "empty" in by["scenarios"].detail


def test_check_skips_non_clickhouse_domains():
    cat = _fake_cat({"fed": SimpleNamespace(source_view="x.y", backend_kind="ibis")})
    ch = _ch_views_present()
    names = {r.name for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=lambda d: cat)}
    assert not any(n.startswith("view:") for n in names)


def test_check_runs_extension_checks_only_when_not_open():
    cat = _fake_cat({})
    ch = _ch_views_present()
    ext = [("planning", lambda d, c: [clickhouse_init.CheckResult("planning:x", True)])]
    all_names = {r.name for r in clickhouse_init.check(Path("/x"), ch, scope="all", _load=lambda d: cat, extension_checks=ext)}
    open_names = {r.name for r in clickhouse_init.check(Path("/x"), ch, scope="open", _load=lambda d: cat, extension_checks=ext)}
    assert "planning:x" in all_names
    assert "planning:x" not in open_names


@pytest.fixture
def _clean_check_registry():
    saved = list(clickhouse_init._EXTENSION_CHECKS)
    clickhouse_init._EXTENSION_CHECKS.clear()
    yield
    clickhouse_init._EXTENSION_CHECKS[:] = saved


def test_register_check_appends_and_dedups(_clean_check_registry):
    clickhouse_init.register_check("planning", lambda d, c: [])
    clickhouse_init.register_check("other", lambda d, c: [])
    clickhouse_init.register_check("planning", lambda d, c: [])
    assert [n for n, _ in clickhouse_init._EXTENSION_CHECKS] == ["planning", "other"]
