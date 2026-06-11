# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Cron scheduler — turns `schedule.mode: cron` bindings into `run_binding`
calls at their declared cron expression.

Stateless across restarts: each tick re-queries `load_history.
last_scheduled_attempt_at(binding_id)` to compute the next fire time, so a
daemon restart never replays a missed window AND never double-fires a window
that already ran. The orchestrator's per-binding advisory lock guards against
concurrent execution if two scheduler replicas happen to tick simultaneously.

Per binding, on a fire-time match:
  1. Resolve the period list via `period_selection.select_periods` — typically
     `lookback`'s trailing-N window so late-arriving postings get re-processed.
  2. For each period (oldest first), call `run_binding(..., "schedule:<binding_id>")`.
  3. Continue past per-period failures — log the attempt's status and move
     on. Operator inspection happens through `load_history`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from precis_mcp.ingestion.orchestrator import (
    LoadAttemptResult,
    OrchestratorContext,
    run_binding,
)
from precis_mcp.ingestion.period_selection import (
    SelectionContext,
    select_periods,
)
from precis_mcp.ingestion.registry import Binding
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.scheduler")


@dataclass
class SchedulerTickResult:
    bindings_inspected: int = 0
    bindings_fired: int = 0
    loads_fired: int = 0
    attempts: list[LoadAttemptResult] = field(default_factory=list)


class Scheduler:
    """Polling cron scheduler. Each `tick()` walks every cron-mode binding,
    decides whether it should fire, and if so dispatches `run_binding` for
    each period the binding's period-selection strategy yields."""

    def __init__(
        self,
        ctx: OrchestratorContext,
        *,
        clock=None,
    ) -> None:
        self.ctx = ctx
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- Public API --------------------------------------------------------

    def tick(self) -> SchedulerTickResult:
        result = SchedulerTickResult()
        now = self._clock()
        for binding in self.ctx.registry.bindings.values():
            if binding.schedule.mode != "cron":
                continue
            result.bindings_inspected += 1
            if self._should_fire(binding, now):
                result.bindings_fired += 1
                self._fire(binding, now, result)
        _logger.info(
            "ingest.scheduler.tick.done",
            bindings_inspected=result.bindings_inspected,
            bindings_fired=result.bindings_fired,
            loads_fired=result.loads_fired,
        )
        return result

    def run_forever(
        self,
        interval_seconds: float,
        *,
        stop_after: Optional[int] = None,
    ) -> None:
        ticks = 0
        while True:
            try:
                self.tick()
            except Exception:
                _logger.exception("ingest.scheduler.tick.failed")
            ticks += 1
            if stop_after is not None and ticks >= stop_after:
                return
            time.sleep(interval_seconds)

    # -- Decision logic ----------------------------------------------------

    def _should_fire(self, binding: Binding, now: datetime) -> bool:
        """Compute whether the binding's cron should fire at or before `now`.

        `next_fire = CronTrigger.get_next_fire_time(last_attempt, last_attempt)`
        returns the next scheduled time AFTER `last_attempt`. If that time is
        <= `now`, the schedule has come due since the previous attempt.
        First-run case: when there's no prior schedule-triggered attempt,
        fire immediately (so a newly-created binding doesn't wait for the
        next cycle).
        """
        try:
            trigger = self._cron_trigger(binding)
        except Exception as exc:
            _logger.warning(
                "ingest.scheduler.bad_cron",
                binding_id=binding.id,
                expression=binding.schedule.expression,
                timezone=binding.schedule.timezone,
                error=str(exc),
            )
            return False

        last_attempt = self.ctx.load_history.last_scheduled_attempt_at(binding.id)
        if last_attempt is None:
            return True
        # Ensure tz-aware comparison.
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        next_fire = trigger.get_next_fire_time(last_attempt, last_attempt)
        if next_fire is None:
            return False
        return next_fire <= now

    def _cron_trigger(self, binding: Binding):
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(
            binding.schedule.expression or "",
            timezone=binding.schedule.timezone or "UTC",
        )

    # -- Firing -----------------------------------------------------------

    def _fire(
        self,
        binding: Binding,
        now: datetime,
        result: SchedulerTickResult,
    ) -> None:
        latest_loaded = self.ctx.load_history.latest_successful_period(binding.id)
        try:
            periods = select_periods(
                binding,
                SelectionContext(
                    now=now,
                    latest_loaded_period=latest_loaded,
                ),
            )
        except Exception as exc:
            _logger.warning(
                "ingest.scheduler.period_selection_failed",
                binding_id=binding.id,
                error=str(exc),
            )
            return

        triggered_by = f"schedule:{binding.id}"
        _logger.info(
            "ingest.scheduler.fire",
            binding_id=binding.id,
            periods=periods,
            now=now.isoformat(),
        )
        for period in periods:
            try:
                attempt = run_binding(self.ctx, binding.id, period, triggered_by)
            except Exception as exc:
                _logger.exception(
                    "ingest.scheduler.run_binding_failed",
                    binding_id=binding.id,
                    period=period,
                )
                continue
            result.loads_fired += 1
            result.attempts.append(attempt)
            if attempt.status != "success":
                _logger.warning(
                    "ingest.scheduler.attempt_non_success",
                    binding_id=binding.id,
                    period=period,
                    load_id=attempt.load_id,
                    status=attempt.status,
                )
