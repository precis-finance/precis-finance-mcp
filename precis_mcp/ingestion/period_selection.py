# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Period selection — given a binding's `schedule.period_selection` config and
a tick time, return the ordered list of periods (`YYYY-MM`) to run.

Period format is the canonical Précis form: a String like `'2026-05'`
(matching `semantic.dim_period.period` and `planning.entries.period` so
that every layer — engine and ingestion — agrees on one shape). Non-
calendar periods (`'2026-13'` for an adjustments period, `'2026-12-ADJ'`
for a closes period) are accepted by the swap and partition layers but
not by the calendar-bound strategies here: `lookback` and `watermark`
only iterate calendar months, so the orchestrator schedules adjustment
periods explicitly through `strategy: explicit`.

Three strategies (period bindings only — snapshot bindings short-circuit to
`[None]` before this helper runs):

- `lookback`: always reload the N trailing periods up to and including the
  tick's period. The default; handles late-arriving GL postings without a
  watermark-tracking concern.
- `explicit`: the trigger supplied a period (push API or admin re-trigger).
  No tick-time inference; the period is what the caller specified.
- `watermark`: pull periods strictly after the last successful load. Subject
  to the watermark-vs-late-arriving trade-off; discouraged for ledger-shaped
  data, available for monotonic event-time sources.

The helper is purely arithmetic over a tick datetime + binding config + (for
watermark) the latest successful `load_history` row. The scheduler is the
only consumer at MVP; the push API takes the explicit period straight from
its request body and bypasses this helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from precis_mcp.ingestion.registry import Binding


__all__ = [
    "PeriodSelectionError",
    "SelectionContext",
    "select_periods",
    "shift_month",
    "datetime_to_period",
    "period_to_first_of_month",
]


class PeriodSelectionError(Exception):
    """Raised when the helper can't resolve a period list (e.g. watermark
    strategy with no prior successful load AND no fallback)."""


@dataclass
class SelectionContext:
    """The state the helper consults beyond the binding's own config.

    `now` — when the scheduler tick fires. The tick's period (`YYYY-MM`
    of `now`) is the inclusive upper bound for `lookback`.
    `explicit_period` — used only when the binding declares
    `strategy: explicit`; otherwise None.
    `latest_loaded_period` — for `strategy: watermark` only; provided by
    the caller (typically from `load_history.find_latest_successful_period`).
    `watermark_fallback_periods` — when watermark mode is configured but no
    prior successful load exists, fall back to this many trailing periods.
    """

    now: datetime
    explicit_period: Optional[str] = None
    latest_loaded_period: Optional[str] = None
    watermark_fallback_periods: int = 3


# ---------------------------------------------------------------------------
# Period arithmetic — strings only; no `dateutil` dependency
# ---------------------------------------------------------------------------


def datetime_to_period(dt: datetime) -> str:
    """`datetime` → canonical `YYYY-MM`. Timezone is ignored — callers
    expected to pass a `now` already in the binding's timezone."""
    return f"{dt.year:04d}-{dt.month:02d}"


def _parse_calendar_period(period: str) -> tuple[int, int]:
    """Parse a calendar `YYYY-MM` period. Raises for non-calendar values
    (`YYYY-13`, `YYYY-MM-ADJ`) — those are handled by `strategy: explicit`
    only and never participate in date arithmetic here."""
    if (
        len(period) != 7
        or period[4] != "-"
        or not period[:4].isdigit()
        or not period[5:].isdigit()
    ):
        raise PeriodSelectionError(
            f"Invalid calendar period {period!r}; expected 'YYYY-MM'"
        )
    year = int(period[:4])
    month = int(period[5:])
    if not 1 <= month <= 12:
        raise PeriodSelectionError(
            f"Non-calendar period {period!r} cannot be used with "
            f"lookback / watermark strategies; use strategy='explicit'"
        )
    return year, month


def period_to_first_of_month(period: str) -> datetime:
    """`YYYY-MM` → `datetime(YYYY, MM, 1, tzinfo=UTC)`. Used for ordering."""
    year, month = _parse_calendar_period(period)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def shift_month(period: str, delta: int) -> str:
    """Return the period `delta` months after (or before, for negative `delta`)."""
    year, month = _parse_calendar_period(period)
    total = (year * 12 + (month - 1)) + delta
    new_year, new_month_idx = divmod(total, 12)
    return f"{new_year:04d}-{new_month_idx + 1:02d}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def select_periods(
    binding: Binding,
    ctx: SelectionContext,
) -> list[Optional[str]]:
    """Return the list of periods the orchestrator should run for this
    binding's current tick. Ordered oldest → newest.

    Snapshot bindings have no period dimension — returns `[None]` (one
    attempt, no period semantics threaded downstream).

    Period-keyed strategies:

    - `lookback`: returns `lookback_periods` periods ending at `ctx.now`.
      `lookback_periods=3` and now=2026-04-15 → ['2026-02', '2026-03', '2026-04'].
    - `explicit`: returns `[ctx.explicit_period]`. Errors if not supplied.
      Accepts non-calendar periods (`'2026-13'`, `'2026-12-ADJ'`) since
      no date arithmetic is performed.
    - `watermark`: returns every period strictly after `latest_loaded_period`
      up to and including the tick's period. If there's no prior load,
      falls back to `watermark_fallback_periods` trailing periods.
    """
    if binding.kind == "snapshot":
        return [None]

    strategy = binding.schedule.period_selection.strategy

    if strategy == "lookback":
        return list(_select_lookback(binding, ctx))
    if strategy == "explicit":
        return list(_select_explicit(ctx))
    if strategy == "watermark":
        return list(_select_watermark(binding, ctx))

    raise PeriodSelectionError(
        f"Unknown period_selection strategy: {strategy!r}"
    )


def _select_lookback(binding: Binding, ctx: SelectionContext) -> list[str]:
    n = binding.schedule.period_selection.lookback_periods
    end = datetime_to_period(ctx.now)
    # Inclusive of the tick's period — N entries ending at `end`.
    return [shift_month(end, -i) for i in range(n - 1, -1, -1)]


def _select_explicit(ctx: SelectionContext) -> list[str]:
    if ctx.explicit_period is None:
        raise PeriodSelectionError(
            "strategy='explicit' requires the caller to provide a period; "
            "pass it on SelectionContext.explicit_period"
        )
    # Explicit accepts any non-empty period token — calendar months,
    # adjustment / 13th periods, etc. No shape validation here.
    if not ctx.explicit_period:
        raise PeriodSelectionError("explicit_period must be a non-empty string")
    return [ctx.explicit_period]


def _select_watermark(binding: Binding, ctx: SelectionContext) -> list[str]:
    tick_period = datetime_to_period(ctx.now)
    if ctx.latest_loaded_period is None:
        # No prior load — fall back to a trailing window so the binding has
        # something to do on its first run.
        n = ctx.watermark_fallback_periods
        return [shift_month(tick_period, -i) for i in range(n - 1, -1, -1)]

    # Walk forward from latest_loaded → tick_period (exclusive of latest,
    # inclusive of tick).
    out: list[str] = []
    cursor = shift_month(ctx.latest_loaded_period, 1)
    end_idx = period_to_first_of_month(tick_period)
    safety = 240  # 20 years of months — guard against pathological gaps
    while period_to_first_of_month(cursor) <= end_idx and safety > 0:
        out.append(cursor)
        cursor = shift_month(cursor, 1)
        safety -= 1
    return out


# ---------------------------------------------------------------------------
# Helper for the scheduler to thread the "latest successful period" lookup
# ---------------------------------------------------------------------------


# `(binding_id) -> Optional[period]`. Injected by the scheduler.
LatestSuccessfulLookup = Callable[[str], Optional[str]]
