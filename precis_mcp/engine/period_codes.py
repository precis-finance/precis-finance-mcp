# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Period-code convention: self-describing time grain via canonical codes.

A period code carries its own grain in its shape, so a filter string alone
determines whether it means a day, week, month, quarter or year. Codes are
**big-endian** (coarsest component leftmost) and fixed-width/zero-padded, which
is what makes a raw lexicographic ``<`` / ``>`` / ``BETWEEN`` a correct range
filter without parsing.

    year     2025            -> (2025,)
    quarter  2025-Q2         -> (2025, 2)
    month    2025-06         -> (2025, 6)
    week     2025-W37        -> (2025, 37)
    date     2025-06-14      -> (2025, 6, 14)

The year is fiscal by default (a natural/calendar-year marker is a forward
extension, not needed while the platform is fiscal-only). This module is pure:
it detects and parses codes; it does not know which grains a domain exposes
(that is the catalogue's job) and it does no time arithmetic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Grain(str, Enum):
    """A time grain, finest to coarsest."""
    DATE = "date"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


# Canonical grammar per grain. Anchored and mutually exclusive, so detection is
# order-independent. Digit fields are fixed-width (zero-padded); range checks
# (month 01-12, week 01-53) are deliberately not enforced here — the calendar
# dimension is the authority on which members exist.
_GRAMMARS: tuple[tuple[Grain, re.Pattern[str]], ...] = (
    (Grain.DATE, re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")),
    (Grain.WEEK, re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")),
    (Grain.MONTH, re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")),
    (Grain.QUARTER, re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$")),
    (Grain.YEAR, re.compile(r"^(?P<year>\d{4})$")),
)

# Component field order per grain, big-endian (coarsest first).
_COMPONENTS: dict[Grain, tuple[str, ...]] = {
    Grain.DATE: ("year", "month", "day"),
    Grain.WEEK: ("year", "week"),
    Grain.MONTH: ("year", "month"),
    Grain.QUARTER: ("year", "quarter"),
    Grain.YEAR: ("year",),
}


@dataclass(frozen=True)
class PeriodCode:
    """A parsed period code: its grain and its big-endian integer components."""
    grain: Grain
    raw: str
    components: tuple[int, ...]  # big-endian, e.g. (2025, 6, 14) for a date

    @property
    def year(self) -> int:
        return self.components[0]


def detect_grain(code: str) -> Grain | None:
    """Return the grain a code denotes, or None if it matches no grammar."""
    for grain, pattern in _GRAMMARS:
        if pattern.match(code):
            return grain
    return None


def parse(code: str) -> PeriodCode | None:
    """Parse a code into its grain and integer components, or None if invalid."""
    for grain, pattern in _GRAMMARS:
        m = pattern.match(code)
        if m:
            components = tuple(int(m.group(f)) for f in _COMPONENTS[grain])
            return PeriodCode(grain=grain, raw=code, components=components)
    return None
