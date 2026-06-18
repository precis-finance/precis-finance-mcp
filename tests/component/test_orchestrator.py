# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the ingestion orchestrator — `run_binding()` end-to-end.

Post-refactor: three stages (`extract → validate → swap`). The
orchestrator owns the load_history skeleton, the Redis lock, the
OTel parent span, and the error-attribution per stage. Stage internals
are delegated to `ibis_executor`, `validate`, and `swap` modules; the
fakes here stand in for the ClickHouse client they all consume, plus
the Ibis backend the executor calls.

No real services touched — `FakeLoadHistoryWriter` (in-memory),
`FakeChClient` (substring-routed `.query()` + capturing `.command()` /
`.insert_df()`), `FakeIbisBackend` (canned DataFrame on
`.sql().execute()`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from precis_mcp.ingestion.orchestrator import (
    LoadAttemptResult,
    OrchestratorContext,
    run_binding,
)
from precis_mcp.ingestion.registry import IntegrationRegistry

from tests.factories.ingestion import build_tree, make_binding, make_source
from tests.fakes.ingestion import (
    FakeChClient,
    FakeIbisBackend,
    FakeLoadHistoryWriter,
    FakeLockFactory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_COLUMNS = [
    ("period", "String"),
    ("account_code", "String"),
    ("cost_centre", "String"),
    ("amount", "Decimal(18, 2)"),
    ("_load_id", "String"),
    ("_ingested_at", "DateTime"),
]


def _seed_matching_shapes(ch: FakeChClient) -> None:
    """Make `validate_staging_shape` see identical columns on both
    `live.fact_gl` and `staging.fact_gl`. Validate routes a single
    `system.columns` query per schema; substring match on the database
    name is enough."""
    ch.set_response("database = 'live'", _DEFAULT_COLUMNS)
    ch.set_response("database = 'staging'", _DEFAULT_COLUMNS)


def _make_context(
    tmp_path: Path,
    *,
    ch_client: Optional[FakeChClient] = None,
    ibis_backend: Optional[FakeIbisBackend] = None,
    lock_available: bool = True,
    source_path: Optional[str] = None,
) -> tuple[OrchestratorContext, FakeLoadHistoryWriter, FakeLockFactory, FakeChClient, FakeIbisBackend]:
    """Build an OrchestratorContext wired against the new pipeline.

    `ibis_backend` defaults to a 3-row DataFrame on `.execute()`, which
    the executor then inserts into staging via `ch_client.insert_df`.
    `ch_client` defaults to one seeded with matching live/staging
    column shapes so validate passes.
    """
    root = build_tree(tmp_path)
    registry = IntegrationRegistry.load(root)

    if ch_client is None:
        ch_client = FakeChClient()
        _seed_matching_shapes(ch_client)
    if ibis_backend is None:
        ibis_backend = FakeIbisBackend(df=pd.DataFrame({
            "period": ["2026-04", "2026-04", "2026-04"],
            "account_code": ["1100", "1100", "1200"],
            "cost_centre": ["CC-01", "CC-02", "CC-01"],
            "amount": [100.00, 200.00, 300.00],
        }))

    history = FakeLoadHistoryWriter()
    lock_factory = FakeLockFactory(available=lock_available)

    ctx = OrchestratorContext(
        registry=registry,
        load_history=history,
        lock_factory=lock_factory,
        ch_client=ch_client,
        ibis_backend_for_source=lambda _src_id: ibis_backend,
        source_path_resolver=lambda _src_id: source_path,
    )
    return ctx, history, lock_factory, ch_client, ibis_backend


# ---------------------------------------------------------------------------
# Happy path — three stages, all clean
# ---------------------------------------------------------------------------


def test_happy_path_runs_extract_validate_swap_in_order(tmp_path: Path):
    ctx, history, lock_factory, _ch, _ibis = _make_context(tmp_path)
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:tester")

    assert isinstance(result, LoadAttemptResult)
    assert result.status == "success"
    assert result.rows_landed == 3

    row = history.rows[result.load_id]
    assert row["status"] == "success"
    assert row["binding_id"] == "test_pg__fact_gl"
    assert row["source_id"] == "test_pg"
    assert row["dataset_id"] == "fact_gl"  # derived from target = live.fact_gl
    assert row["period"] == "2026-04"
    assert row["scenario_id"] == "ACTUALS"
    assert row["triggered_by"] == "admin:tester"
    assert row["rows_landed"] == 3
    assert "swap_committed_at" in row

    # Event order in load_history: insert → extract → swap → final.
    # validate is a structural gate; it does not emit its own
    # load_history record (orchestrator gates on the ValidationResult).
    event_kinds = [e.split(":")[0] for e in history.events]
    assert event_kinds == ["insert", "extract", "swap", "final"]

    assert lock_factory.last_lock is not None
    assert lock_factory.last_lock.acquired_count == 1
    assert lock_factory.last_lock.released_count == 1


def test_happy_path_substitutes_period_into_extract_query(tmp_path: Path):
    """`:period` is substituted with a quoted literal before the Ibis
    backend sees the SQL. The factory's default query has
    `WHERE period = :period`."""
    ctx, _h, _lf, _ch, ibis = _make_context(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    assert ibis.queries == [
        "SELECT period, account_code, cost_centre, amount "
        "FROM gl.journal_postings WHERE period = '2026-04'"
    ]


def test_happy_path_swap_emits_replace_partition_then_drop(tmp_path: Path):
    """For period bindings, swap fires REPLACE PARTITION on live then
    DROP PARTITION on staging. The DROP PARTITION before INSERT in
    extract is the idempotency clear for staging."""
    ctx, _h, _lf, ch, _ibis = _make_context(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    # Last two ch.commands are the swap commands; the order is
    # REPLACE PARTITION then staging DROP PARTITION.
    replace_idx = next(
        i for i, c in enumerate(ch.commands)
        if "REPLACE PARTITION '2026-04'" in c
    )
    drop_idx = next(
        i for i, c in enumerate(ch.commands)
        if i > replace_idx
        and "DROP PARTITION '2026-04'" in c
        and "staging.fact_gl" in c
    )
    assert replace_idx < drop_idx
    assert "live.fact_gl" in ch.commands[replace_idx]


def test_distinct_load_ids_across_runs(tmp_path: Path):
    ctx, _h, _lf, _ch, _ibis = _make_context(tmp_path)
    r1 = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    r2 = run_binding(ctx, "test_pg__fact_gl", "2026-05", "admin:t")
    assert r1.load_id != r2.load_id


# ---------------------------------------------------------------------------
# Lock conflict
# ---------------------------------------------------------------------------


def test_lock_conflict_aborts_with_failed_other(tmp_path: Path):
    ctx, history, lock_factory, _ch, _ibis = _make_context(
        tmp_path, lock_available=False
    )
    history.rows["pre-existing"] = {
        "load_id": "pre-existing",
        "binding_id": "test_pg__fact_gl",
        "status": "running",
        "source_id": "test_pg",
        "dataset_id": "fact_gl",
        "period": "2026-04",
        "scenario_id": "ACTUALS",
        "triggered_by": "schedule:test_pg__fact_gl",
    }

    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_other"
    assert "Lock conflict" in (result.error or "")
    assert "pre-existing" in (result.error or "")
    assert history.rows[result.load_id]["status"] == "failed_other"
    assert lock_factory.last_lock is not None
    assert lock_factory.last_lock.acquired_count == 0


# ---------------------------------------------------------------------------
# Extract failure
# ---------------------------------------------------------------------------


def test_extract_failure_releases_lock_and_marks_failed_extract(tmp_path: Path):
    ibis = FakeIbisBackend(exc=RuntimeError("source unreachable"))
    ctx, history, lock_factory, _ch, _ibis = _make_context(
        tmp_path, ibis_backend=ibis
    )
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_extract"
    assert "source unreachable" in (result.error or "")
    row = history.rows[result.load_id]
    assert row["status"] == "failed_extract"
    # The orchestrator never reached record_extract.
    assert "extract" not in [e.split(":")[0] for e in history.events]
    assert lock_factory.last_lock is not None
    assert lock_factory.last_lock.released_count == 1


def test_extract_failure_does_not_call_swap(tmp_path: Path):
    ibis = FakeIbisBackend(exc=RuntimeError("source unreachable"))
    ctx, _h, _lf, ch, _ibis = _make_context(tmp_path, ibis_backend=ibis)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    # No REPLACE PARTITION command issued.
    assert not any("REPLACE PARTITION" in c for c in ch.commands)


def test_zero_row_extract_refused_before_swap(tmp_path: Path):
    """A 0-row extract must not swap: REPLACE PARTITION from an empty
    staging partition would wipe the live period while reporting success."""
    ibis = FakeIbisBackend(df=pd.DataFrame({
        "period": [], "account_code": [], "cost_centre": [], "amount": [],
    }))
    ctx, history, lock_factory, ch, _ibis = _make_context(
        tmp_path, ibis_backend=ibis
    )
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_validation"
    assert result.rows_landed == 0
    assert "0 rows" in (result.error or "")
    assert history.rows[result.load_id]["status"] == "failed_validation"
    assert not any("REPLACE PARTITION" in c for c in ch.commands)
    assert lock_factory.last_lock is not None
    assert lock_factory.last_lock.released_count == 1


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


def test_shape_drift_short_circuits_before_swap_with_failed_recon(
    tmp_path: Path,
):
    """If staging's column shape diverges from live (e.g. a column
    was added on one side but not the other), the validator catches
    it and the orchestrator aborts before swap with
    `failed_recon` — the legacy bucket reused for the validation gate
    so we don't need a load_history CHECK migration.
    """
    ch = FakeChClient()
    # Mismatched shapes: live has 'amount' Decimal(18,2), staging has
    # 'amount' Decimal(10,2) — type mismatch.
    ch.set_response(
        "database = 'live'",
        [(c[0], c[1]) for c in _DEFAULT_COLUMNS],
    )
    ch.set_response(
        "database = 'staging'",
        [
            ("period", "String"),
            ("account_code", "String"),
            ("cost_centre", "String"),
            ("amount", "Decimal(10, 2)"),  # ← diverges
            ("_load_id", "String"),
            ("_ingested_at", "DateTime"),
        ],
    )
    ctx, history, _lf, _ch, _ibis = _make_context(tmp_path, ch_client=ch)
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_recon"
    assert "Validation failed" in (result.error or "")
    assert history.rows[result.load_id]["status"] == "failed_recon"
    # Swap never fired.
    assert not any("REPLACE PARTITION" in c for c in ch.commands)


# ---------------------------------------------------------------------------
# Swap failure
# ---------------------------------------------------------------------------


def test_swap_failure_marks_failed_swap_does_not_record_swap(tmp_path: Path):
    ch = FakeChClient()
    _seed_matching_shapes(ch)
    ch.fail_command_with = RuntimeError("ClickHouse unreachable")
    ch.fail_command_matching = "REPLACE PARTITION"  # swap-only; let extract land
    ctx, history, _lf, _ch, _ibis = _make_context(tmp_path, ch_client=ch)
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_swap"
    assert "ClickHouse unreachable" in (result.error or "")
    row = history.rows[result.load_id]
    assert row["status"] == "failed_swap"
    assert "swap_committed_at" not in row


# ---------------------------------------------------------------------------
# Lock lifecycle across every exit path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["happy", "extract_fail", "validate_fail", "swap_fail"],
)
def test_lock_always_released(tmp_path: Path, scenario: str):
    if scenario == "extract_fail":
        ibis = FakeIbisBackend(exc=RuntimeError("x"))
        ctx, _h, lf, _ch, _ibis = _make_context(tmp_path, ibis_backend=ibis)
    elif scenario == "validate_fail":
        ch = FakeChClient()
        # Returning empty rows for live makes validate raise
        # ValidationError ("table missing") — separate from the
        # mismatch test above; both should release the lock.
        ch.set_response("database = 'live'", [])
        ch.set_response("database = 'staging'", _DEFAULT_COLUMNS)
        ctx, _h, lf, _ch, _ibis = _make_context(tmp_path, ch_client=ch)
    elif scenario == "swap_fail":
        ch = FakeChClient()
        _seed_matching_shapes(ch)
        ch.fail_command_with = RuntimeError("x")
        ctx, _h, lf, _ch, _ibis = _make_context(tmp_path, ch_client=ch)
    else:
        ctx, _h, lf, _ch, _ibis = _make_context(tmp_path)

    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    assert lf.last_lock is not None
    assert lf.last_lock.released_count == 1


# ---------------------------------------------------------------------------
# Snapshot binding — kind contract + EXCHANGE TABLES dispatch
# ---------------------------------------------------------------------------


def test_snapshot_binding_uses_exchange_tables_not_replace_partition(
    tmp_path: Path,
):
    src = make_source("test_pg")
    bd = make_binding(
        binding_id="test_pg__dim_client",
        source="test_pg",
        target="live.dim_client",
        kind="snapshot",
    )
    root = build_tree(tmp_path, sources=[src], bindings=[bd])
    registry = IntegrationRegistry.load(root)

    ch = FakeChClient()
    ch.set_response("database = 'live'", [("client_id", "String")])
    ch.set_response("database = 'staging'", [("client_id", "String")])
    ibis = FakeIbisBackend(df=pd.DataFrame({"client_id": ["c1", "c2"]}))

    history = FakeLoadHistoryWriter()
    ctx = OrchestratorContext(
        registry=registry,
        load_history=history,
        lock_factory=FakeLockFactory(),
        ch_client=ch,
        ibis_backend_for_source=lambda _: ibis,
        source_path_resolver=lambda _: None,
    )
    result = run_binding(ctx, "test_pg__dim_client", None, "admin:t")
    assert result.status == "success"
    assert any("EXCHANGE TABLES" in c for c in ch.commands)
    assert not any("REPLACE PARTITION" in c for c in ch.commands)


def test_period_binding_rejects_missing_period(tmp_path: Path):
    ctx, _h, _lf, _ch, _ibis = _make_context(tmp_path)
    with pytest.raises(ValueError, match="kind='period'"):
        run_binding(ctx, "test_pg__fact_gl", None, "admin:t")


def test_snapshot_binding_rejects_period(tmp_path: Path):
    bd = make_binding(
        binding_id="test_pg__dim_client",
        source="test_pg",
        target="live.dim_client",
        kind="snapshot",
    )
    root = build_tree(tmp_path, sources=[make_source("test_pg")], bindings=[bd])
    registry = IntegrationRegistry.load(root)
    ctx = OrchestratorContext(
        registry=registry,
        load_history=FakeLoadHistoryWriter(),
        lock_factory=FakeLockFactory(),
        ch_client=FakeChClient(),
        ibis_backend_for_source=lambda _: FakeIbisBackend(),
        source_path_resolver=lambda _: None,
    )
    with pytest.raises(ValueError, match="kind='snapshot'"):
        run_binding(ctx, "test_pg__dim_client", "2026-04", "admin:t")


# ---------------------------------------------------------------------------
# Data-quality checks (the validate-stage control gate)
# ---------------------------------------------------------------------------


def _ctx_with_checks(tmp_path: Path, checks: list[dict]):
    """An orchestrator context whose single binding carries `checks`.
    Shapes are seeded so structural validation passes — the checks are
    what the test exercises."""
    bd = {**make_binding(), "checks": checks}
    registry = IntegrationRegistry.load(build_tree(tmp_path, bindings=[bd]))
    ch = FakeChClient()
    _seed_matching_shapes(ch)
    ibis = FakeIbisBackend(df=pd.DataFrame({
        "period": ["2026-04", "2026-04", "2026-04"],
        "account_code": ["1100", "1100", "1200"],
        "cost_centre": ["CC-01", "CC-02", "CC-01"],
        "amount": [100.00, 200.00, 300.00],
    }))
    history = FakeLoadHistoryWriter()
    ctx = OrchestratorContext(
        registry=registry,
        load_history=history,
        lock_factory=FakeLockFactory(available=True),
        ch_client=ch,
        ibis_backend_for_source=lambda _s: ibis,
        source_path_resolver=lambda _s: None,
    )
    return ctx, history, ch, ibis


def test_error_check_blocks_swap(tmp_path: Path):
    ctx, history, ch, _ibis = _ctx_with_checks(
        tmp_path,
        [{"name": "amt", "type": "not_null", "column": "amount", "severity": "error"}],
    )
    ch.set_response("amount is null", [(3,)])  # 3 null amounts → error trips
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "failed_checks"
    assert not any("REPLACE PARTITION" in c for c in ch.commands)  # no swap
    assert "checks" in [e.split(":")[0] for e in history.events]
    ctr = history.rows[result.load_id]["control_total_result"]
    assert ctr["verdict"] == "failed_checks"
    assert ctr["checks"][0] == {
        "name": "amt", "severity": "error", "type": "not_null",
        "passed": False, "failing": 3,
    }


def test_warning_check_loads_with_warnings(tmp_path: Path):
    ctx, history, ch, _ibis = _ctx_with_checks(
        tmp_path,
        [{"name": "sign", "type": "expression", "expression": "amount >= 0",
          "severity": "warning"}],
    )
    ch.set_response("not (amount >= 0)", [(2,)])  # 2 negatives → warns, loads
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    assert result.status == "success"                       # warnings still land
    assert any("REPLACE PARTITION" in c for c in ch.commands)  # swap happened
    ctr = history.rows[result.load_id]["control_total_result"]
    assert ctr["verdict"] == "succeeded_with_warnings"


def test_reconcile_source_query_captured_during_extract(tmp_path: Path):
    ctx, _history, _ch, ibis = _ctx_with_checks(
        tmp_path,
        [{"name": "recon", "type": "reconcile", "group_by": ["period"],
          "measures": {"amount_sum": {"expr": "sum(amount)", "tolerance": {"abs": 0.01}}},
          "source_query": "SELECT period, SUM(amount) AS amount_sum FROM tb "
                          "WHERE period = :period GROUP BY 1",
          "severity": "info"}],  # info → never gates; we assert the capture only
    )
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    # The reconcile source_query is run against the source during extract,
    # with :period bound — the authoritative total to tie staged data to.
    assert any(
        "FROM tb WHERE period = '2026-04'" in q for q in ibis.queries
    )
