# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for `precis_mcp/ingestion/scheduler.py` — the ingestion-side cron
scheduler that turns `schedule.mode: cron` bindings into `run_binding` calls.

Different subsystem from the routines/dispatch scheduler at
`tests/component/test_scheduler.py`. Uses the shared ingestion fakes for
the orchestrator context dependencies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.scheduler import Scheduler
from tests.factories.ingestion import (
    build_tree,
    make_binding,
    make_orchestrator_context,
    make_source,
)
from tests.fakes.ingestion import FakeIbisBackend


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _registry_with_cron_binding(
    tmp_path: Path,
    *,
    binding_id: str = "manual_drop__gl",
    expression: str = "0 2 * * *",
    timezone_str: str = "UTC",
    lookback_periods: int = 1,
):
    src = make_source()
    bd = make_binding(binding_id=binding_id)
    bd["schedule"] = {
        "mode": "cron",
        "expression": expression,
        "timezone": timezone_str,
        "period_selection": {
            "strategy": "lookback",
            "lookback_periods": lookback_periods,
        },
    }
    root = build_tree(tmp_path, sources=[src], bindings=[bd])
    return IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )


def _ctx(registry, *, clock=None):
    """Build an OrchestratorContext + matching FakeLoadHistoryWriter.

    Pass `clock` so the fake's `started_at` recording is in the same time
    coordinate as the Scheduler's `clock` — otherwise scheduler tests that
    consult `last_scheduled_attempt_at` see real wall-clock timestamps from
    the fake but mocked timestamps from the scheduler, and the cron
    comparison goes haywire.
    """
    ctx, history, _ch, _ibis = make_orchestrator_context(registry, clock=clock)
    return ctx, history


# ---------------------------------------------------------------------------
# tick — happy paths
# ---------------------------------------------------------------------------


def test_tick_fires_on_first_run_when_no_prior_attempt(tmp_path: Path):
    """No `last_scheduled_attempt_at` → fire immediately on first tick."""
    registry = _registry_with_cron_binding(tmp_path, lookback_periods=1)
    ctx, history = _ctx(registry)
    sched = Scheduler(
        ctx, clock=lambda: datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    )

    result = sched.tick()
    assert result.bindings_inspected == 1
    assert result.bindings_fired == 1
    assert result.loads_fired == 1
    # The single lookback period == the tick period.
    assert result.attempts[0].load_id in history.rows
    assert history.rows[result.attempts[0].load_id]["period"] == "2026-04"
    assert history.rows[result.attempts[0].load_id]["triggered_by"].startswith(
        "schedule:"
    )


def test_tick_fires_multiple_periods_for_lookback(tmp_path: Path):
    """`lookback_periods=3` and a tick in April → fires Feb, Mar, Apr."""
    registry = _registry_with_cron_binding(tmp_path, lookback_periods=3)
    ctx, history = _ctx(registry)
    sched = Scheduler(
        ctx, clock=lambda: datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    )
    result = sched.tick()
    assert result.loads_fired == 3
    periods = sorted(history.rows[a.load_id]["period"] for a in result.attempts)
    assert periods == ["2026-02", "2026-03", "2026-04"]


def test_tick_does_not_fire_when_next_cron_is_in_the_future(tmp_path: Path):
    """Cron is daily 02:00. If the last attempt was 30 minutes ago, the next
    fire is ~24h out — don't fire on this tick."""
    registry = _registry_with_cron_binding(
        tmp_path, expression="0 2 * * *", timezone_str="UTC"
    )
    ctx, history = _ctx(registry)
    now = datetime(2026, 4, 15, 2, 30, tzinfo=timezone.utc)

    # Seed a recent scheduled attempt.
    last_attempt_at = now - timedelta(minutes=30)
    history.rows["seed"] = {
        "load_id": "seed",
        "binding_id": "manual_drop__gl",
        "status": "success",
        "started_at": last_attempt_at,
        "triggered_by": "schedule:manual_drop__gl",
        "source_id": "manual_drop",
        "dataset_id": "gl",
        "period": "2026-04",
        "scenario_id": "ACTUALS",
    }

    sched = Scheduler(ctx, clock=lambda: now)
    result = sched.tick()
    assert result.bindings_inspected == 1
    assert result.bindings_fired == 0
    assert result.loads_fired == 0


def test_tick_fires_when_next_cron_is_in_the_past(tmp_path: Path):
    """Last attempt 2 days ago + daily-02:00 cron → next-fire is in the past,
    so this tick fires."""
    registry = _registry_with_cron_binding(
        tmp_path, expression="0 2 * * *", timezone_str="UTC"
    )
    ctx, history = _ctx(registry)
    now = datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    history.rows["seed"] = {
        "load_id": "seed",
        "binding_id": "manual_drop__gl",
        "status": "success",
        "started_at": now - timedelta(days=2),
        "triggered_by": "schedule:manual_drop__gl",
        "source_id": "manual_drop",
        "dataset_id": "gl",
        "period": "2026-04",
        "scenario_id": "ACTUALS",
    }

    sched = Scheduler(ctx, clock=lambda: now)
    result = sched.tick()
    assert result.bindings_fired == 1
    assert result.loads_fired == 1


# ---------------------------------------------------------------------------
# Multi-replica / stateless behaviour
# ---------------------------------------------------------------------------


def test_second_tick_does_not_re_fire_after_first_succeeds(tmp_path: Path):
    """First tick fires; the resulting attempt's `started_at` is fresh, so
    the second tick (one second later) sees no new fire is due."""
    registry = _registry_with_cron_binding(
        tmp_path, expression="0 2 * * *", timezone_str="UTC"
    )

    # Single mutable clock used by both the scheduler and the fake writer so
    # `started_at` timestamps land in the same coordinate as scheduler ticks.
    clock_holder = {"now": datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)}

    def clock():
        return clock_holder["now"]

    ctx, _history = _ctx(registry, clock=clock)
    sched = Scheduler(ctx, clock=clock)

    first = sched.tick()
    clock_holder["now"] = datetime(2026, 4, 15, 3, 0, 1, tzinfo=timezone.utc)
    second = sched.tick()
    assert first.loads_fired >= 1
    assert second.loads_fired == 0


# ---------------------------------------------------------------------------
# Non-cron bindings are skipped
# ---------------------------------------------------------------------------


def test_tick_skips_watch_and_push_bindings(tmp_path: Path):
    src = make_source()
    watch_bd = make_binding(binding_id="manual_drop__gl", schedule_mode="watch")
    push_bd = make_binding(binding_id="manual_drop__ap", target="live.fact_ap")
    push_bd["schedule"] = {
        "mode": "push",
        "push_auth": {"role": "ingest_runner", "binding_token_ref": "t"},
    }
    root = build_tree(
        tmp_path,
        sources=[src],
        bindings=[watch_bd, push_bd],
    )
    registry = IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )
    ctx, _ = _ctx(registry)
    sched = Scheduler(
        ctx, clock=lambda: datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    )
    result = sched.tick()
    assert result.bindings_inspected == 0  # neither was cron
    assert result.loads_fired == 0


# ---------------------------------------------------------------------------
# Resilience to bad cron expressions
# ---------------------------------------------------------------------------


def test_tick_skips_binding_with_bad_cron_expression(tmp_path: Path):
    registry = _registry_with_cron_binding(
        tmp_path, expression="not a cron expr"
    )
    ctx, _history = _ctx(registry)
    sched = Scheduler(
        ctx, clock=lambda: datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    )
    result = sched.tick()
    # Bad cron → bindings_inspected counts it but bindings_fired stays 0.
    assert result.bindings_inspected == 1
    assert result.bindings_fired == 0
    assert result.loads_fired == 0


# ---------------------------------------------------------------------------
# Driver-error tolerance
# ---------------------------------------------------------------------------


def test_tick_continues_when_one_period_fails(tmp_path: Path):
    """A single period's `run_binding` failure should not abort the rest of
    the tick — the scheduler logs and moves on."""
    registry = _registry_with_cron_binding(tmp_path, lookback_periods=2)
    ctx, _history = _ctx(registry)

    # Make the extract backend raise on every extract.
    failing = FakeIbisBackend(exc=RuntimeError("transient extract failure"))
    ctx.ibis_backend_for_source = lambda _src: failing

    sched = Scheduler(
        ctx, clock=lambda: datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
    )
    result = sched.tick()
    # Both periods attempted; both ended in failed_extract status — the
    # scheduler still completes its tick cleanly.
    assert result.loads_fired == 2
    assert all(a.status == "failed_extract" for a in result.attempts)
