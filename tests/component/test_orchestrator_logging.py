# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the orchestrator's structured-logging contract.

Three-stage pipeline: every load attempt emits at minimum
  - ingest.attempt.start
  - ingest.extract.done
  - ingest.validate.done
  - ingest.swap.done
  - ingest.attempt.done

Failure paths emit a stage-specific `*.failed` log instead of the
corresponding `*.done`, and no `ingest.attempt.done` (that's reserved
for success). Lock conflicts emit `ingest.attempt.lock_conflict`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from precis_mcp.ingestion import orchestrator as orch_module
from precis_mcp.ingestion.orchestrator import OrchestratorContext, run_binding
from precis_mcp.ingestion.registry import IntegrationRegistry

from tests.factories.ingestion import build_tree
from tests.fakes.ingestion import (
    FakeChClient,
    FakeIbisBackend,
    FakeLoadHistoryWriter,
    FakeLockFactory,
)


# ---------------------------------------------------------------------------
# Logger capture
# ---------------------------------------------------------------------------


class CapturingLogger:
    """Records each logger call so tests can assert on event names + kwargs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str):
        def inner(event: str, **kwargs: Any) -> None:
            self.events.append((level, event, kwargs))
        return inner

    def __getattr__(self, level: str):
        return self._record(level)


@pytest.fixture
def capture_logger(monkeypatch):
    cap = CapturingLogger()
    monkeypatch.setattr(orch_module, "_logger", cap)
    return cap


def _events_named(cap: CapturingLogger, name: str) -> list[dict[str, Any]]:
    return [kw for _lvl, ev, kw in cap.events if ev == name]


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------


_MATCHING_SHAPE = [
    ("period", "String"),
    ("account_code", "String"),
    ("cost_centre", "String"),
    ("amount", "Decimal(18, 2)"),
    ("_load_id", "String"),
    ("_ingested_at", "DateTime"),
]


def _make_ctx(tmp_path: Path) -> OrchestratorContext:
    root = build_tree(tmp_path)
    registry = IntegrationRegistry.load(root)
    ch = FakeChClient()
    ch.set_response("database = 'live'", _MATCHING_SHAPE)
    ch.set_response("database = 'staging'", _MATCHING_SHAPE)
    ibis = FakeIbisBackend(
        df=pd.DataFrame({
            "period": ["2026-04"],
            "account_code": ["1100"],
            "cost_centre": ["CC-01"],
            "amount": [100.0],
        }),
    )
    return OrchestratorContext(
        registry=registry,
        load_history=FakeLoadHistoryWriter(),
        lock_factory=FakeLockFactory(available=True),
        ch_client=ch,
        ibis_backend_for_source=lambda _: ibis,
        source_path_resolver=lambda _: None,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_emits_three_stage_event_sequence(tmp_path: Path, capture_logger):
    ctx = _make_ctx(tmp_path)
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")

    required = [
        "ingest.attempt.start",
        "ingest.extract.done",
        "ingest.validate.done",
        "ingest.swap.done",
        "ingest.attempt.done",
    ]
    event_names = [ev for _lvl, ev, _kw in capture_logger.events]
    for ev in required:
        assert ev in event_names, f"missing event: {ev}"
    # Order: start precedes done.
    assert event_names.index("ingest.attempt.start") < event_names.index(
        "ingest.attempt.done"
    )

    done = _events_named(capture_logger, "ingest.attempt.done")[0]
    assert done["status"] == "success"
    assert done["load_id"] == result.load_id


def test_attempt_start_carries_required_fields(tmp_path: Path, capture_logger):
    ctx = _make_ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:tester")
    start = _events_named(capture_logger, "ingest.attempt.start")
    assert len(start) == 1
    fields = start[0]
    for field in (
        "load_id",
        "binding_id",
        "source_id",
        "target",
        "period",
        "scenario_id",
        "triggered_by",
    ):
        assert field in fields, f"ingest.attempt.start missing {field}"
    assert fields["binding_id"] == "test_pg__fact_gl"
    assert fields["target"] == "live.fact_gl"
    assert fields["period"] == "2026-04"
    assert fields["triggered_by"] == "admin:tester"


def test_extract_done_includes_rows_landed_and_duration(
    tmp_path: Path, capture_logger
):
    ctx = _make_ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    extract = _events_named(capture_logger, "ingest.extract.done")
    assert len(extract) == 1
    assert "rows_landed" in extract[0]
    assert "duration_ms" in extract[0]


def test_validate_done_includes_binding_id(tmp_path: Path, capture_logger):
    ctx = _make_ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    done = _events_named(capture_logger, "ingest.validate.done")
    assert len(done) == 1
    assert done[0]["binding_id"] == "test_pg__fact_gl"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_extract_failure_emits_extract_failed_no_done(tmp_path: Path, capture_logger):
    ctx = _make_ctx(tmp_path)
    ctx.ibis_backend_for_source = lambda _: FakeIbisBackend(
        exc=RuntimeError("api down")
    )
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    assert len(_events_named(capture_logger, "ingest.extract.failed")) == 1
    assert _events_named(capture_logger, "ingest.extract.done") == []
    # Failure paths do not emit attempt.done — that's the success-only event.
    assert _events_named(capture_logger, "ingest.attempt.done") == []


def test_validate_failure_emits_validate_failed_no_swap_done(
    tmp_path: Path, capture_logger
):
    """Shape drift → validate raises → orchestrator marks failed_recon
    and skips swap. The orchestrator's `ingest.validate.failed` fires;
    swap-stage events do not."""
    root = build_tree(tmp_path)
    registry = IntegrationRegistry.load(root)
    ch = FakeChClient()
    ch.set_response("database = 'live'", _MATCHING_SHAPE)
    ch.set_response("database = 'staging'", [("period", "String")])  # drift
    ctx = OrchestratorContext(
        registry=registry,
        load_history=FakeLoadHistoryWriter(),
        lock_factory=FakeLockFactory(),
        ch_client=ch,
        ibis_backend_for_source=lambda _: FakeIbisBackend(
            df=pd.DataFrame({
                "period": ["2026-04"], "account_code": ["1"],
                "cost_centre": ["c"], "amount": [1.0],
            }),
        ),
        source_path_resolver=lambda _: None,
    )
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    assert len(_events_named(capture_logger, "ingest.validate.failed")) == 1
    assert _events_named(capture_logger, "ingest.swap.done") == []
    assert _events_named(capture_logger, "ingest.attempt.done") == []


def test_swap_failure_emits_swap_failed_no_attempt_done(
    tmp_path: Path, capture_logger
):
    ctx = _make_ctx(tmp_path)
    ctx.ch_client.fail_command_with = RuntimeError("ClickHouse offline")  # type: ignore[attr-defined]
    ctx.ch_client.fail_command_matching = "REPLACE PARTITION"  # type: ignore[attr-defined]
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    assert len(_events_named(capture_logger, "ingest.swap.failed")) == 1
    assert _events_named(capture_logger, "ingest.attempt.done") == []


def test_lock_conflict_emits_lock_conflict_event(tmp_path: Path, capture_logger):
    ctx = _make_ctx(tmp_path)
    ctx.lock_factory = FakeLockFactory(available=False)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "admin:t")
    conflicts = _events_named(capture_logger, "ingest.attempt.lock_conflict")
    assert len(conflicts) == 1
    assert conflicts[0]["binding_id"] == "test_pg__fact_gl"
