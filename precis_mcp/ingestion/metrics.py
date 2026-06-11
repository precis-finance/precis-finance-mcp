# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Per-binding ingestion metrics.

Instruments declared once at import time, populated lazily by the OTel meter.
The orchestrator imports the module-level emit helpers below; they're
attribute-cheap (a dict per call) and noop when OTel isn't configured.

Metric names follow the `precis.<subsystem>.<concept>` convention; tag keys
match the structured-log attribute names (`binding`, `status`, `stage`) so a
metric drill-down in Grafana lines up with a log search in Phoenix.

Three instruments (Phase 3 of `docs/ingestion_transform_unification_spec.md`
dropped `precis.ingest.recon_failures_total` along with the reconciliation
module; dbt test failures surface in dbt's own per-test result JSON, not as
a Précis-side counter):

    precis.ingest.attempts_total          counter   {binding, status}
    precis.ingest.duration_seconds        histogram {binding, stage}
    precis.ingest.rows_landed             histogram {binding}
"""

from __future__ import annotations

from precis_mcp.observability import get_meter


_meter = get_meter("precis_mcp.ingestion")

_attempts_total = _meter.create_counter(
    "precis.ingest.attempts_total",
    description="Count of ingestion attempts terminated, by binding and final status.",
)

_duration_seconds = _meter.create_histogram(
    "precis.ingest.duration_seconds",
    description="Wall-clock duration of each ingestion stage, by binding and stage.",
    unit="s",
)

_rows_landed = _meter.create_histogram(
    "precis.ingest.rows_landed",
    description="Row count landed in staging per attempt, by binding.",
)


# ---------------------------------------------------------------------------
# Public emit helpers — keep call-site noise low
# ---------------------------------------------------------------------------


def record_attempt(binding_id: str, status: str) -> None:
    """Record one terminal attempt. Call from the orchestrator's finalise."""
    _attempts_total.add(1, {"binding": binding_id, "status": status})


def record_stage_duration(binding_id: str, stage: str, duration_seconds: float) -> None:
    """Record one stage's wall-clock duration in seconds."""
    _duration_seconds.record(
        max(0.0, duration_seconds),
        {"binding": binding_id, "stage": stage},
    )


def record_rows_landed(binding_id: str, rows: int) -> None:
    """Record the row count delivered to staging on a successful extract."""
    if rows < 0:
        return  # Defensive — driver shouldn't report negatives.
    _rows_landed.record(rows, {"binding": binding_id})


# ---------------------------------------------------------------------------
# Test seam — replace the module-level instruments with capturing ones so
# component tests can assert on the emissions without booting an OTel SDK.
# ---------------------------------------------------------------------------


def _install_instruments_for_tests(
    *,
    attempts_total=None,
    duration_seconds=None,
    rows_landed=None,
) -> None:
    """Replace the module-level instruments (intended for tests).

    Each parameter, if provided, must expose `.add(value, attrs)` for
    counters or `.record(value, attrs)` for histograms — same shape as the
    OTel instruments. Pass None to leave an instrument unchanged.
    """
    global _attempts_total, _duration_seconds, _rows_landed
    if attempts_total is not None:
        _attempts_total = attempts_total
    if duration_seconds is not None:
        _duration_seconds = duration_seconds
    if rows_landed is not None:
        _rows_landed = rows_landed
