# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Period inference helpers.

The watcher service uses `infer_period_from_filename` when `binding.schedule.
watch.period_from == "filename_regex"` to derive the period from an arriving
file's key. `infer_period_from_rows` handles the `column-derived` mode for
sources where the file name doesn't carry the period.

Outputs are always the canonical `YYYY-MM` string Précis uses everywhere
(matching `semantic.dim_period.period` and the binding's
`partition_expression` literal). Adjustment / 13th periods
(`'2026-13'`, `'2026-12-ADJ'`) flow through `normalise_period` unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional


class PeriodInferenceError(Exception):
    """Raised when the period can't be derived from the file or rows."""


# Compact 6-digit form (legacy / external systems) — convert to dashed.
_YYYYMM_RE = re.compile(r"^(\d{4})(\d{2})$")
# Canonical dashed form, plus optional adjustment-period suffix.
_DASHED_RE = re.compile(r"^\d{4}-(\d{2}|\d{2}-[A-Z]+)$")
# Calendar date form — keep only the first two date components.
_DATE_RE = re.compile(r"^(\d{4})[-/](\d{2})")


def normalise_period(raw: str) -> str:
    """Coerce a period-looking string into canonical `YYYY-MM`.

    Accepted inputs: `2026-04`, `202604`, `2026/04`, `2026-04-01`, plus
    explicit adjustment / 13th periods (`'2026-13'`, `'2026-12-ADJ'`)
    which pass through verbatim. Anything else raises
    `PeriodInferenceError`.
    """
    raw = raw.strip()
    if _DASHED_RE.match(raw):
        return raw
    m = _YYYYMM_RE.match(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _DATE_RE.match(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    raise PeriodInferenceError(
        f"Cannot normalise period: {raw!r} — expected 'YYYY-MM', 'YYYYMM', "
        f"'YYYY-MM-DD', or a Précis adjustment period like 'YYYY-13' / 'YYYY-MM-ADJ'"
    )


def infer_period_from_filename(filename: str, regex: str) -> str:
    """Apply the binding's `filename_regex` to a file name and return the
    captured period (named group `period`)."""
    pattern = re.compile(regex)
    m = pattern.search(filename)
    if m is None:
        raise PeriodInferenceError(
            f"filename_regex {regex!r} did not match {filename!r}"
        )
    try:
        period_raw = m.group("period")
    except IndexError as exc:
        raise PeriodInferenceError(
            f"filename_regex {regex!r} matched but has no named group 'period'"
        ) from exc
    return normalise_period(period_raw)


def infer_period_from_rows(
    rows: Iterable[dict[str, Any]],
    column: str,
) -> str:
    """Inspect the first row and read `column` to derive the period.

    For our scope this assumes all rows in a file are for the same period
    (monthly grain GL is the common case). If a file mixes periods, the
    range_check control total will catch it post-load.
    """
    first_row: Optional[dict[str, Any]] = None
    for row in rows:
        first_row = row
        break
    if first_row is None:
        raise PeriodInferenceError("Cannot infer period from empty row stream")
    if column not in first_row:
        raise PeriodInferenceError(
            f"period column {column!r} not found in row: {sorted(first_row)}"
        )
    value = first_row[column]
    if value is None:
        raise PeriodInferenceError(f"period column {column!r} is null in first row")
    return normalise_period(str(value))
