# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Orchestrator — `run_binding(binding_id, period, triggered_by)` end-to-end.

The single entrypoint that cron, push, and watch triggers all converge on.
Three stages, executed in order:

    extract   →   validate   →   swap
       ↓             ↓             ↓
ibis_executor.   validate.     swap.swap
   execute       validate_     (REPLACE PARTITION or
                 staging_       EXCHANGE TABLES)
                 shape

The orchestrator is the glue holding the three stage modules together —
plus the load_history, lock, span, and error-attribution scaffolding.

Failure modes
-------------
Each stage has its own status bucket on `load_history`:

  extract  failure →  `failed_extract`
  zero-row extract → `failed_validation` (refused before swap; an empty
                                         staging partition would wipe the
                                         live partition)
  validate failure →  `failed_recon`    (the validation-failure status
                                         bucket)
  swap     failure →  `failed_swap`
  lock     conflict → `failed_other`

`success` is set only after a clean swap.
"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Protocol

from precis_mcp.ingestion import checks as dq_checks
from precis_mcp.ingestion import metrics as ingest_metrics
from precis_mcp.ingestion.ibis_executor import _render_source_query
from precis_mcp.ingestion.ibis_executor import execute as ibis_execute
from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.swap import swap as do_swap
from precis_mcp.ingestion.validate import (
    ValidationError,
    validate_staging_shape,
)
from precis_mcp.observability import get_logger, get_tracer

_logger = get_logger("ingestion.orchestrator")
_tracer = get_tracer("precis_mcp.ingestion.orchestrator")


# ---------------------------------------------------------------------------
# Stage span + duration metric helper
# ---------------------------------------------------------------------------


@contextmanager
def _stage(name: str, binding_id: str, attributes: Optional[dict] = None):
    """Open an OTel span `ingest.<stage>` and emit a duration histogram
    on exit. Errors propagate; the span records them and sets status to
    ERROR before re-raising.
    """
    start = time.monotonic()
    span_name = f"ingest.{name}"
    try:
        from opentelemetry.trace import Status, StatusCode  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — OTel optional
        Status = None  # type: ignore[assignment]
        StatusCode = None  # type: ignore[assignment]
    with _tracer.start_as_current_span(span_name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            if Status is not None and StatusCode is not None:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            ingest_metrics.record_stage_duration(
                binding_id, name, time.monotonic() - start
            )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class LoadAttemptResult:
    """The orchestrator's per-attempt return shape."""

    load_id: str
    status: str  # mirrors load_history.status
    rows_landed: Optional[int] = None
    error: Optional[str] = None
    duration_ms: int = 0
    stopped_after: Optional[str] = None


# ---------------------------------------------------------------------------
# Persistence interface — keeps tests free of a real Postgres
# ---------------------------------------------------------------------------


class LoadHistoryWriter(Protocol):
    """Persistence interface for `load_history` rows. Production wiring
    routes through `precis_mcp.db.execute_platform`; tests pass an
    in-memory fake.
    """

    def insert_attempt(
        self,
        load_id: str,
        binding_id: str,
        source_id: str,
        dataset_id: str,
        period: str,
        scenario_id: str,
        triggered_by: str,
        notes: Optional[str] = None,
    ) -> None: ...

    def record_extract(
        self,
        load_id: str,
        rows_landed: int,
        source_manifest: dict[str, Any],
    ) -> None: ...

    def record_swap(self, load_id: str) -> None: ...

    def record_checks(
        self, load_id: str, control_total_result: dict[str, Any]
    ) -> None: ...

    def finalise(
        self,
        load_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None: ...

    def find_running_for_binding(self, binding_id: str) -> Optional[str]: ...

    def processed_watch_keys_for_binding(self, binding_id: str) -> set[str]: ...

    def last_scheduled_attempt_at(self, binding_id: str) -> Optional[Any]: ...

    def latest_successful_period(self, binding_id: str) -> Optional[str]: ...


# ---------------------------------------------------------------------------
# Lock — abstracted so tests don't need a real backend
# ---------------------------------------------------------------------------


class LockHandle(Protocol):
    def acquire(self, blocking: bool = True) -> bool: ...
    def release(self) -> None: ...


class LockFactory(Protocol):
    def __call__(
        self, key: str, *, timeout: int, blocking_timeout: int
    ) -> LockHandle: ...


# ---------------------------------------------------------------------------
# Context — bag of dependencies
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorContext:
    """Dependencies the orchestrator needs.

    `ibis_backend_for_source(source_id)` returns the Ibis connection
    for a given source. Wiring resolves this via the shared
    `IbisRegistry` — same registry the federated read path uses, so
    credentials and connection pools are unified.

    `source_path_resolver(source_id)` returns an absolute path string
    for file-drop sources (resolved from the source's `backend.prefix`
    + `PRECIS_INGEST_UPLOAD_DIR`). Returns None for warehouse sources.
    The orchestrator threads the value into the executor's
    `template_vars={"source_path": ...}` so bindings can write
    `read_csv('${source_path}/<file>.csv')`.

    `ch_client` is a clickhouse-connect Client passed to ibis_executor
    (for staging writes), validate (for system.columns reads), and
    swap (for REPLACE PARTITION / EXCHANGE TABLES).
    """

    registry: IntegrationRegistry
    load_history: LoadHistoryWriter
    lock_factory: LockFactory
    ch_client: Any
    ibis_backend_for_source: Callable[[str], Any]
    source_path_resolver: Callable[[str], Optional[str]] = lambda _: None

    lock_timeout_seconds: int = 600
    lock_blocking_timeout_seconds: int = 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _new_load_id() -> str:
    """Monotonic-then-random identifier. ULID-shaped without the
    dependency. Sortable lexicographically by start time; uniqueness
    from the random suffix.
    """
    return f"{time.time_ns():020d}-{secrets.token_hex(8)}"


StageName = Literal["extract", "validate", "swap"]
_STAGE_ORDER: tuple[StageName, ...] = ("extract", "validate", "swap")


def _early_finalise(
    ctx: "OrchestratorContext",
    load_id: str,
    stage: StageName,
    rows_landed: Optional[int],
    started: float,
    binding_id: str,
) -> "LoadAttemptResult":
    """Finalise as `success` with `stopped_after=<stage>` when the
    operator invoked `run_binding(stop_after=<stage>)` and that stage
    just completed. Production triggers don't use stop_after; this is a
    debugging knob.
    """
    ctx.load_history.finalise(load_id, "success", error_message=None)
    ingest_metrics.record_attempt(binding_id, "success")
    duration = int((time.monotonic() - started) * 1000)
    _logger.info(
        "ingest.attempt.stopped",
        load_id=load_id,
        binding_id=binding_id,
        stage=stage,
        rows_landed=rows_landed,
        duration_ms=duration,
    )
    return LoadAttemptResult(
        load_id=load_id,
        status="success",
        rows_landed=rows_landed,
        duration_ms=duration,
        stopped_after=stage,
    )


def run_binding(
    ctx: OrchestratorContext,
    binding_id: str,
    period: Optional[str],
    triggered_by: str,
    *,
    notes: Optional[str] = None,
    stop_after: Optional[StageName] = None,
) -> LoadAttemptResult:
    """Execute one `(binding × period)` load end-to-end. Single-threaded;
    the advisory lock serialises concurrent triggers for the same binding.

    For snapshot bindings (`binding.kind == 'snapshot'`), pass
    `period=None`. The snapshot's `load_history.period` records the
    current `'YYYY-MM'` as a "when was the snapshot taken" stamp so the
    non-null + format CHECK on `load_history.period` holds for snapshot
    loads too.

    `notes` is an optional operator audit text (admin re-trigger flow).

    `stop_after` is the operator-debugging knob: run stages up to and
    including the named stage (`extract`, `validate`, or `swap`), then
    return. Production triggers leave it at default `None` = full
    pipeline.
    """
    if stop_after is not None and stop_after not in _STAGE_ORDER:
        raise ValueError(
            f"stop_after={stop_after!r} must be one of "
            f"{list(_STAGE_ORDER)} or None"
        )
    started = time.monotonic()

    binding = ctx.registry.get_binding(binding_id)
    source = ctx.registry.get_source(binding.source)

    if binding.kind == "snapshot" and period is not None:
        raise ValueError(
            f"Binding {binding_id!r} is kind='snapshot'; period must be None"
        )
    if binding.kind == "period" and period is None:
        raise ValueError(
            f"Binding {binding_id!r} is kind='period'; period is required"
        )

    # load_history.period is NOT NULL with a CHECK constraint accepting
    # 'YYYY-...' shape. For snapshot bindings, stamp wall-clock month.
    history_period = (
        period if period is not None
        else time.strftime("%Y-%m", time.gmtime())
    )

    load_id = _new_load_id()
    ctx.load_history.insert_attempt(
        load_id=load_id,
        binding_id=binding_id,
        source_id=source.id,
        dataset_id=binding.table_name,
        period=history_period,
        scenario_id=binding.scenario,
        triggered_by=triggered_by,
        notes=notes,
    )

    # Parent span across the whole attempt; per-stage child spans nest
    # inside.
    parent_span_attrs = {
        "ingest.load_id": load_id,
        "ingest.binding_id": binding_id,
        "ingest.source_id": source.id,
        "ingest.source.kind": source.kind,
        "ingest.target": binding.target,
        "ingest.period": period,
        "ingest.scenario_id": binding.scenario,
        "db.system": source.kind,
        "db.connection.name": source.id,
    }
    with _tracer.start_as_current_span("ingest.attempt") as attempt_span:
        for k, v in parent_span_attrs.items():
            attempt_span.set_attribute(k, v)

        _logger.info(
            "ingest.attempt.start",
            load_id=load_id,
            binding_id=binding_id,
            source_id=source.id,
            target=binding.target,
            period=period,
            scenario_id=binding.scenario,
            triggered_by=triggered_by,
        )

        lock_key = f"ingest_lock:{binding_id}"
        lock = ctx.lock_factory(
            lock_key,
            timeout=ctx.lock_timeout_seconds,
            blocking_timeout=ctx.lock_blocking_timeout_seconds,
        )
        if not lock.acquire(blocking=True):
            in_flight = ctx.load_history.find_running_for_binding(binding_id)
            msg = (
                f"Lock conflict for binding {binding_id!r}; in-flight "
                f"load: {in_flight!r}"
            )
            ctx.load_history.finalise(load_id, "failed_other", error_message=msg)
            _logger.warning(
                "ingest.attempt.lock_conflict",
                load_id=load_id,
                binding_id=binding_id,
                in_flight=in_flight,
            )
            ingest_metrics.record_attempt(binding_id, "failed_other")
            return LoadAttemptResult(
                load_id=load_id,
                status="failed_other",
                error=msg,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        reconcile_source_totals: dict[str, list[dict]] = {}
        try:
            # ----- Stage 1: extract -----
            try:
                with _stage("extract", binding_id):
                    ibis_backend = ctx.ibis_backend_for_source(source.id)
                    source_path = ctx.source_path_resolver(source.id)
                    template_vars = (
                        {"source_path": source_path} if source_path else None
                    )
                    extract_result = ibis_execute(
                        binding_id=binding.id,
                        source_id=source.id,
                        kind=binding.kind,
                        period=period,
                        target=binding.target,
                        extract_query=binding.extract.query,
                        load_id=load_id,
                        ibis_backend=ibis_backend,
                        ch_client=ctx.ch_client,
                        template_vars=template_vars,
                    )
                    # Capture each reconcile check's authoritative source total
                    # while the source connection is open — the figure the
                    # staged data must tie to. A source-read failure here is a
                    # legitimate extract failure (can't read the source total).
                    for rc in dq_checks.reconcile_checks(binding):
                        rendered = _render_source_query(
                            rc.source_query, period, template_vars
                        )
                        df = ibis_backend.sql(rendered).execute()
                        reconcile_source_totals[rc.name] = (
                            df.to_dict("records")
                            if hasattr(df, "to_dict") else list(df)
                        )
            except Exception as exc:
                msg = f"Extract failed: {exc}"
                _logger.exception(
                    "ingest.extract.failed",
                    load_id=load_id,
                    binding_id=binding_id,
                )
                ctx.load_history.finalise(load_id, "failed_extract", error_message=msg)
                ingest_metrics.record_attempt(binding_id, "failed_extract")
                return LoadAttemptResult(
                    load_id=load_id,
                    status="failed_extract",
                    error=msg,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            ctx.load_history.record_extract(
                load_id, extract_result.rows_landed, {}
            )
            ingest_metrics.record_rows_landed(
                binding_id, extract_result.rows_landed
            )
            attempt_span.set_attribute(
                "ingest.rows_landed", extract_result.rows_landed
            )
            _logger.info(
                "ingest.extract.done",
                load_id=load_id,
                rows_landed=extract_result.rows_landed,
                duration_ms=extract_result.duration_ms,
            )

            if stop_after == "extract":
                return _early_finalise(
                    ctx, load_id, "extract",
                    extract_result.rows_landed, started, binding_id,
                )

            # ----- Zero-row guard -----
            # Swapping an empty staging partition would REPLACE the live
            # partition with nothing: a broken extract (wrong period filter,
            # source outage) must not silently wipe a period of live data
            # while reporting success.
            if extract_result.rows_landed == 0:
                msg = (
                    f"Extract returned 0 rows for period {period!r}; "
                    "refusing to swap an empty partition over live data."
                )
                _logger.warning(
                    "ingest.zero_rows.refused",
                    load_id=load_id,
                    binding_id=binding_id,
                    period=period,
                )
                ctx.load_history.finalise(
                    load_id, "failed_validation", error_message=msg
                )
                ingest_metrics.record_attempt(binding_id, "failed_validation")
                return LoadAttemptResult(
                    load_id=load_id,
                    status="failed_validation",
                    rows_landed=0,
                    error=msg,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            # ----- Stage 2: validate -----
            try:
                with _stage("validate", binding_id):
                    vr = validate_staging_shape(
                        target=binding.target,
                        ch_client=ctx.ch_client,
                    )
                    if not vr.passed:
                        raise RuntimeError(
                            f"Shape validation failed:\n{vr.summary()}"
                        )
            except (ValidationError, RuntimeError) as exc:
                msg = f"Validation failed: {exc}"
                _logger.exception(
                    "ingest.validate.failed",
                    load_id=load_id,
                    binding_id=binding_id,
                )
                ctx.load_history.finalise(load_id, "failed_recon", error_message=msg)
                ingest_metrics.record_attempt(binding_id, "failed_recon")
                return LoadAttemptResult(
                    load_id=load_id,
                    status="failed_recon",
                    rows_landed=extract_result.rows_landed,
                    error=msg,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            _logger.info(
                "ingest.validate.done",
                load_id=load_id,
                binding_id=binding_id,
            )

            # ----- Stage 2b: data-quality checks (operator-declared) -----
            # Structural shape has passed — a precondition, since the check
            # SQL assumes the columns exist and are typed right. The
            # operator's business-rule checks now run against staging and the
            # swap is gated on the combined outcome.
            if binding.checks:
                try:
                    check_run = dq_checks.run_checks(
                        binding,
                        ch_client=ctx.ch_client,
                        reconcile_source_totals=reconcile_source_totals,
                    )
                except Exception as exc:
                    msg = f"Data-quality checks could not run: {exc}"
                    _logger.exception(
                        "ingest.checks.errored",
                        load_id=load_id,
                        binding_id=binding_id,
                    )
                    ctx.load_history.finalise(
                        load_id, "failed_recon", error_message=msg
                    )
                    ingest_metrics.record_attempt(binding_id, "failed_recon")
                    return LoadAttemptResult(
                        load_id=load_id,
                        status="failed_recon",
                        rows_landed=extract_result.rows_landed,
                        error=msg,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                ctx.load_history.record_checks(load_id, check_run.to_jsonb())
                _logger.info(
                    "ingest.checks.done",
                    load_id=load_id,
                    binding_id=binding_id,
                    verdict=check_run.verdict,
                    summary=check_run.summary(),
                )
                if check_run.blocked:
                    msg = f"Data-quality checks failed: {check_run.summary()}"
                    ctx.load_history.finalise(
                        load_id, "failed_checks", error_message=msg
                    )
                    ingest_metrics.record_attempt(binding_id, "failed_checks")
                    return LoadAttemptResult(
                        load_id=load_id,
                        status="failed_checks",
                        rows_landed=extract_result.rows_landed,
                        error=msg,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

            if stop_after == "validate":
                return _early_finalise(
                    ctx, load_id, "validate",
                    extract_result.rows_landed, started, binding_id,
                )

            # ----- Stage 3: swap -----
            try:
                with _stage("swap", binding_id, attributes={"ingest.period": period}):
                    do_swap(
                        kind=binding.kind,
                        period=period,
                        target=binding.target,
                        load_id=load_id,
                        ch_client=ctx.ch_client,
                    )
            except Exception as exc:
                msg = f"Swap failed: {exc}"
                _logger.exception("ingest.swap.failed", load_id=load_id)
                ctx.load_history.finalise(load_id, "failed_swap", error_message=msg)
                ingest_metrics.record_attempt(binding_id, "failed_swap")
                return LoadAttemptResult(
                    load_id=load_id,
                    status="failed_swap",
                    rows_landed=extract_result.rows_landed,
                    error=msg,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            ctx.load_history.record_swap(load_id)
            _logger.info("ingest.swap.done", load_id=load_id, period=period)

            # ----- Finalise -----
            ctx.load_history.finalise(load_id, "success")
            ingest_metrics.record_attempt(binding_id, "success")
            duration = int((time.monotonic() - started) * 1000)
            _logger.info(
                "ingest.attempt.done",
                load_id=load_id,
                status="success",
                rows_landed=extract_result.rows_landed,
                duration_ms=duration,
            )
            return LoadAttemptResult(
                load_id=load_id,
                status="success",
                rows_landed=extract_result.rows_landed,
                duration_ms=duration,
            )
        finally:
            try:
                lock.release()
            except Exception:
                # Releasing an expired lock is benign; never let a
                # release error take down the orchestrator.
                _logger.debug(
                    "ingest.attempt.lock_release_noop", load_id=load_id
                )