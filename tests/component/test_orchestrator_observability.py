# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the orchestrator's observability emissions — OpenTelemetry
spans + per-binding metrics.

Three-stage pipeline (`extract → validate → swap`). The metric
instruments (`attempts_total`, `duration_seconds`, `rows_landed`)
emit per stage / per binding; the parent `ingest.attempt` span carries
the load identity, with per-stage child spans nested under it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from precis_mcp.ingestion import metrics as ingest_metrics
from precis_mcp.ingestion import orchestrator as orchestrator_module
from precis_mcp.ingestion.orchestrator import OrchestratorContext, run_binding
from precis_mcp.ingestion.registry import IntegrationRegistry
from tests.factories.ingestion import build_tree
from tests.fakes.ingestion import (
    FakeChClient,
    FakeIbisBackend,
    FakeLoadHistoryWriter,
    FakeLockFactory,
)


_MATCHING_SHAPE = [
    ("period", "String"),
    ("account_code", "String"),
    ("cost_centre", "String"),
    ("amount", "Decimal(18, 2)"),
    ("_load_id", "String"),
    ("_ingested_at", "DateTime"),
]


# ---------------------------------------------------------------------------
# Capturing fakes for metric instruments
# ---------------------------------------------------------------------------


class _CapturingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict]] = []

    def add(self, value: int, attrs: dict) -> None:
        self.calls.append((value, attrs))


class _CapturingHistogram:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict]] = []

    def record(self, value: float, attrs: dict) -> None:
        self.calls.append((value, attrs))


@pytest.fixture
def captured_metrics():
    """Swap the module-level metric instruments with capturing ones for
    one test; restore on teardown."""
    captures = {
        "attempts": _CapturingCounter(),
        "duration": _CapturingHistogram(),
        "rows": _CapturingHistogram(),
    }
    original = (
        ingest_metrics._attempts_total,  # type: ignore[attr-defined]
        ingest_metrics._duration_seconds,  # type: ignore[attr-defined]
        ingest_metrics._rows_landed,  # type: ignore[attr-defined]
    )
    ingest_metrics._install_instruments_for_tests(  # type: ignore[attr-defined]
        attempts_total=captures["attempts"],
        duration_seconds=captures["duration"],
        rows_landed=captures["rows"],
    )
    try:
        yield captures
    finally:
        ingest_metrics._install_instruments_for_tests(  # type: ignore[attr-defined]
            attempts_total=original[0],
            duration_seconds=original[1],
            rows_landed=original[2],
        )


# ---------------------------------------------------------------------------
# Capturing tracer
# ---------------------------------------------------------------------------


class _CapturingSpan:
    def __init__(self, name: str, parent: "_CapturingTracer") -> None:
        self.name = name
        self.attrs: dict = {}
        self.exceptions: list[Exception] = []
        self.status = None
        self._parent = parent

    def set_attribute(self, key: str, value) -> None:
        self.attrs[key] = value

    def record_exception(self, exc: Exception) -> None:
        self.exceptions.append(exc)

    def set_status(self, status) -> None:
        self.status = status

    def add_event(self, *args, **kwargs):
        pass

    def __enter__(self):
        self._parent.entered.append(self.name)
        return self

    def __exit__(self, *exc):
        self._parent.exited.append(self.name)
        return False


class _CapturingTracer:
    def __init__(self) -> None:
        self.spans: list[_CapturingSpan] = []
        self.entered: list[str] = []
        self.exited: list[str] = []

    def start_as_current_span(self, name: str, *args, **kwargs):
        span = _CapturingSpan(name, self)
        self.spans.append(span)
        return span


@pytest.fixture
def captured_tracer(monkeypatch):
    tracer = _CapturingTracer()
    monkeypatch.setattr(orchestrator_module, "_tracer", tracer)
    return tracer


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------


def _ctx(
    tmp_path: Path,
    *,
    ibis_exc: Exception | None = None,
    shape_drift: bool = False,
):
    root = build_tree(tmp_path)
    registry = IntegrationRegistry.load(root)
    ch = FakeChClient()
    if shape_drift:
        ch.set_response("database = 'live'", _MATCHING_SHAPE)
        ch.set_response("database = 'staging'", [("period", "String")])
    else:
        ch.set_response("database = 'live'", _MATCHING_SHAPE)
        ch.set_response("database = 'staging'", _MATCHING_SHAPE)

    if ibis_exc is not None:
        ibis = FakeIbisBackend(exc=ibis_exc)
    else:
        ibis = FakeIbisBackend(df=pd.DataFrame({
            "period": ["2026-04"] * 14250,
            "account_code": ["1100"] * 14250,
            "cost_centre": ["CC-01"] * 14250,
            "amount": [1.0] * 14250,
        }))
    ctx = OrchestratorContext(
        registry=registry,
        load_history=FakeLoadHistoryWriter(),
        lock_factory=FakeLockFactory(available=True),
        ch_client=ch,
        ibis_backend_for_source=lambda _: ibis,
        source_path_resolver=lambda _: None,
    )
    return ctx


# ---------------------------------------------------------------------------
# Metrics — happy path
# ---------------------------------------------------------------------------


def test_happy_path_emits_attempt_success_counter(tmp_path: Path, captured_metrics):
    ctx = _ctx(tmp_path)
    result = run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    assert result.status == "success"

    attempts = captured_metrics["attempts"].calls
    assert len(attempts) == 1
    value, attrs = attempts[0]
    assert value == 1
    assert attrs == {"binding": "test_pg__fact_gl", "status": "success"}


def test_happy_path_emits_rows_landed_histogram(tmp_path: Path, captured_metrics):
    ctx = _ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    rows = captured_metrics["rows"].calls
    assert len(rows) == 1
    value, attrs = rows[0]
    assert value == 14250
    assert attrs == {"binding": "test_pg__fact_gl"}


def test_happy_path_emits_per_stage_duration_histograms(
    tmp_path: Path, captured_metrics
):
    """Each of the three stages (extract, validate, swap) records one
    duration sample."""
    ctx = _ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    durations = captured_metrics["duration"].calls
    stages_recorded = {attrs["stage"] for _v, attrs in durations}
    assert stages_recorded == {"extract", "validate", "swap"}
    assert all(attrs["binding"] == "test_pg__fact_gl" for _v, attrs in durations)
    assert all(v >= 0 for v, _attrs in durations)


# ---------------------------------------------------------------------------
# Metrics — failure paths
# ---------------------------------------------------------------------------


def test_extract_failure_emits_failed_extract_attempt(
    tmp_path: Path, captured_metrics
):
    ctx = _ctx(tmp_path, ibis_exc=RuntimeError("vendor api down"))
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    attempts = captured_metrics["attempts"].calls
    assert len(attempts) == 1
    value, attrs = attempts[0]
    assert value == 1
    assert attrs["status"] == "failed_extract"
    # No rows-landed metric on failed extract.
    assert captured_metrics["rows"].calls == []
    # Extract duration is still emitted (the stage ran, even if it failed).
    durations = captured_metrics["duration"].calls
    stages = {attrs["stage"] for _v, attrs in durations}
    assert "extract" in stages
    assert "validate" not in stages
    assert "swap" not in stages


def test_validate_failure_emits_failed_recon_attempt(
    tmp_path: Path, captured_metrics
):
    """Shape drift on staging vs live → orchestrator marks the load
    `failed_recon` (legacy bucket reused for the validation gate so the
    load_history CHECK constraint accepts it without a migration)."""
    ctx = _ctx(tmp_path, shape_drift=True)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    attempts = captured_metrics["attempts"].calls
    assert any(attrs["status"] == "failed_recon" for _v, attrs in attempts)


# ---------------------------------------------------------------------------
# OpenTelemetry spans
# ---------------------------------------------------------------------------


def test_happy_path_emits_parent_attempt_span_with_required_attrs(
    tmp_path: Path, captured_tracer
):
    ctx = _ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    attempt_spans = [s for s in captured_tracer.spans if s.name == "ingest.attempt"]
    assert len(attempt_spans) == 1
    attrs = attempt_spans[0].attrs
    for key in (
        "ingest.load_id",
        "ingest.binding_id",
        "ingest.source_id",
        "ingest.source.kind",
        "ingest.target",
        "ingest.period",
        "ingest.scenario_id",
        "db.system",
        "db.connection.name",
    ):
        assert key in attrs, f"parent span missing attribute {key}"
    assert attrs["ingest.binding_id"] == "test_pg__fact_gl"
    assert attrs["ingest.target"] == "live.fact_gl"
    assert attrs["ingest.period"] == "2026-04"
    # rows_landed gets set on the parent after extract returns.
    assert attrs["ingest.rows_landed"] == 14250


def test_happy_path_opens_child_span_per_stage(
    tmp_path: Path, captured_tracer
):
    """Three child spans, one per stage: extract → validate → swap."""
    ctx = _ctx(tmp_path)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    span_names = [s.name for s in captured_tracer.spans]
    assert "ingest.extract" in span_names
    assert "ingest.validate" in span_names
    assert "ingest.swap" in span_names


def test_extract_span_records_exception_on_extract_failure(
    tmp_path: Path, captured_tracer
):
    ctx = _ctx(tmp_path, ibis_exc=RuntimeError("api down"))
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    extract_span = next(
        s for s in captured_tracer.spans if s.name == "ingest.extract"
    )
    assert len(extract_span.exceptions) == 1
    assert "api down" in str(extract_span.exceptions[0])


def test_validate_span_records_exception_on_shape_drift(
    tmp_path: Path, captured_tracer
):
    """The validate child span captures the validation failure so
    operators can trace which binding's shape drifted without digging
    into structlog."""
    ctx = _ctx(tmp_path, shape_drift=True)
    run_binding(ctx, "test_pg__fact_gl", "2026-04", "test")
    validate_span = next(
        s for s in captured_tracer.spans if s.name == "ingest.validate"
    )
    assert len(validate_span.exceptions) == 1
