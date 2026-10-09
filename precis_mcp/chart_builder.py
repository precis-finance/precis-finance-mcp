# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure chart block and caption construction shared with commercial views."""
from __future__ import annotations

def _dedup_preserving_order(items) -> list:
    seen: set = set()
    out: list = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def consolidate_captions(captions: list) -> dict | None:
    """Merge per-source captions into one display caption.

    Display-only and lossy by design (the machine record is ``source``, kept
    separate). A single caption passes through unchanged. For multiple:
    scenarios/dimensions are unioned, filters merged, ``scale`` kept only when
    all sources agree (mixed units move to the axis labels), and ``period``
    surfaced verbatim when shared — or labelled per source when it differs, so
    a period mismatch is never silently collapsed into a false comparison.
    """
    captions = [c for c in captions if isinstance(c, dict) and c]
    if not captions:
        return None
    if len(captions) == 1:
        return dict(captions[0])

    merged: dict = {}

    scenarios = _dedup_preserving_order(
        s for c in captions for s in (c.get("scenarios") or [])
    )
    if scenarios:
        merged["scenarios"] = scenarios

    dimensions = _dedup_preserving_order(
        d for c in captions for d in (c.get("dimensions") or [])
    )
    if dimensions:
        merged["dimensions"] = dimensions

    filters: dict = {}
    for c in captions:
        f = c.get("filters")
        if isinstance(f, dict):
            filters.update(f)
    if filters:
        merged["filters"] = filters

    # scale: keep only if every source agrees; otherwise it's irreconcilable
    # across units and belongs on the (per-axis) labels, not the caption.
    scales = _dedup_preserving_order(
        c.get("scale") for c in captions if c.get("scale")
    )
    if len(scales) == 1:
        merged["scale"] = scales[0]

    # period: shared → verbatim; divergent → label each by its description so
    # the mismatch is visible rather than averaged away.
    periods = _dedup_preserving_order(
        c.get("period") for c in captions if c.get("period")
    )
    if len(periods) == 1:
        merged["period"] = periods[0]
    elif len(periods) > 1:
        labelled = []
        for c in captions:
            p = c.get("period")
            if not p:
                continue
            desc = c.get("description")
            labelled.append(f"{desc}: {p}" if desc else p)
        merged["period"] = " · ".join(_dedup_preserving_order(labelled))

    durations = [
        d for c in captions
        if isinstance((d := c.get("duration_ms")), (int, float))
    ]
    if durations:
        merged["duration_ms"] = sum(durations)

    return merged



def build_chart_block(result: dict) -> dict | None:
    if result.get("status") != "ok" or not isinstance(result.get("chart"), dict):
        return None
    return {**result["chart"], "type": "chart"}
