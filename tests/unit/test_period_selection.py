# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for `precis_mcp/ingestion/period_selection.py` — period helpers and
the scheduler's `select_periods()` strategy dispatch. Pure logic; no I/O."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from precis_mcp.ingestion.period_selection import (
    PeriodSelectionError,
    SelectionContext,
    datetime_to_period,
    period_to_first_of_month,
    select_periods,
    shift_month,
)
from precis_mcp.ingestion.registry import Binding
from tests.factories.ingestion import make_binding


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dt_args,expected",
    [
        ((2026, 4, 15, 12, 30), "2026-04"),
        ((2026, 1, 1), "2026-01"),
        ((2025, 12, 31), "2025-12"),
    ],
)
def test_datetime_to_period(dt_args, expected):
    assert datetime_to_period(datetime(*dt_args, tzinfo=timezone.utc)) == expected


def test_period_to_first_of_month():
    dt = period_to_first_of_month("2026-04")
    assert dt.year == 2026 and dt.month == 4 and dt.day == 1


@pytest.mark.parametrize(
    "bad",
    [
        "20260",         # wrong length
        "202604",        # no dash (legacy YYYYMM)
        "2026/04",       # wrong separator
        "abc-de",        # non-digits
        "2026-13",       # non-calendar period — explicit strategy only
        "2026-04-ADJ",   # adjustment period — explicit strategy only
        "",
    ],
)
def test_period_to_first_of_month_rejects_bad(bad):
    with pytest.raises(PeriodSelectionError):
        period_to_first_of_month(bad)


@pytest.mark.parametrize(
    "start,delta,expected",
    [
        ("2026-04", 0, "2026-04"),
        ("2026-04", 1, "2026-05"),
        ("2026-04", -1, "2026-03"),
        ("2026-12", 1, "2027-01"),  # December → next-year January
        ("2026-01", -1, "2025-12"),  # January → prior-year December
        ("2026-04", 12, "2027-04"),
        ("2026-04", -24, "2024-04"),
    ],
)
def test_shift_month(start, delta, expected):
    assert shift_month(start, delta) == expected


def test_shift_month_rejects_bad_period():
    with pytest.raises(PeriodSelectionError):
        shift_month("not_a_period", 1)


# ---------------------------------------------------------------------------
# select_periods — lookback strategy
# ---------------------------------------------------------------------------


def _binding(
    strategy: str = "lookback",
    lookback_periods: int = 3,
) -> Binding:
    spec = make_binding(schedule_mode="cron", kind="period")
    spec["schedule"]["period_selection"] = {
        "strategy": strategy,
        "lookback_periods": lookback_periods,
    }
    return Binding.model_validate(spec)


def test_lookback_returns_trailing_window_inclusive_of_tick():
    binding = _binding("lookback", lookback_periods=3)
    ctx = SelectionContext(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
    assert select_periods(binding, ctx) == ["2026-02", "2026-03", "2026-04"]


def test_lookback_one_period_returns_only_tick_period():
    binding = _binding("lookback", lookback_periods=1)
    ctx = SelectionContext(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
    assert select_periods(binding, ctx) == ["2026-04"]


def test_lookback_crosses_year_boundary():
    binding = _binding("lookback", lookback_periods=3)
    ctx = SelectionContext(now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert select_periods(binding, ctx) == ["2025-11", "2025-12", "2026-01"]


# ---------------------------------------------------------------------------
# select_periods — explicit strategy
# ---------------------------------------------------------------------------


def test_explicit_returns_only_the_supplied_period():
    binding = _binding("explicit")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        explicit_period="2026-02",
    )
    assert select_periods(binding, ctx) == ["2026-02"]


def test_explicit_without_supplied_period_raises():
    binding = _binding("explicit")
    ctx = SelectionContext(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
    with pytest.raises(PeriodSelectionError, match="explicit_period"):
        select_periods(binding, ctx)


def test_explicit_passes_through_adjustment_periods():
    """Adjustment / 13th periods (`'2026-13'`, `'2026-12-ADJ'`) flow
    through `strategy: explicit` unchanged — no calendar arithmetic
    happens here, and the partition / swap layer accepts arbitrary
    String period literals."""
    binding = _binding("explicit")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        explicit_period="2026-13",
    )
    assert select_periods(binding, ctx) == ["2026-13"]


def test_explicit_rejects_empty_period():
    binding = _binding("explicit")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        explicit_period="",
    )
    with pytest.raises(PeriodSelectionError):
        select_periods(binding, ctx)


# ---------------------------------------------------------------------------
# select_periods — watermark strategy
# ---------------------------------------------------------------------------


def test_watermark_returns_periods_strictly_after_last_loaded():
    binding = _binding("watermark")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        latest_loaded_period="2026-01",
    )
    # Excludes 2026-01, includes 2026-02..2026-04
    assert select_periods(binding, ctx) == ["2026-02", "2026-03", "2026-04"]


def test_watermark_no_prior_load_falls_back_to_trailing_window():
    binding = _binding("watermark")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        latest_loaded_period=None,
        watermark_fallback_periods=2,
    )
    # Falls back to 2 trailing periods ending at the tick.
    assert select_periods(binding, ctx) == ["2026-03", "2026-04"]


def test_watermark_caught_up_returns_empty_list():
    """When latest_loaded equals the tick period, there's nothing new to load."""
    binding = _binding("watermark")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        latest_loaded_period="2026-04",
    )
    assert select_periods(binding, ctx) == []


def test_watermark_with_gap_walks_forward_one_month_at_a_time():
    """latest_loaded six months behind → list all six interim periods."""
    binding = _binding("watermark")
    ctx = SelectionContext(
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        latest_loaded_period="2025-10",
    )
    assert select_periods(binding, ctx) == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
    ]


# ---------------------------------------------------------------------------
# Unknown strategy
# ---------------------------------------------------------------------------


def test_unknown_strategy_raises():
    binding = _binding("lookback")
    # Mutate after construction to bypass the pydantic Literal[...] enum.
    binding.schedule.period_selection.strategy = "ghost"  # type: ignore[assignment]
    ctx = SelectionContext(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
    with pytest.raises(PeriodSelectionError, match="Unknown"):
        select_periods(binding, ctx)
