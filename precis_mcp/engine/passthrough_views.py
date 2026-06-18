# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Auto-generate trivial pass-through semantic views for catalogue dimensions.

Under the standard `live → semantic → catalogue`, the catalogue references
`semantic.*` only. A leaf dimension that needs no transform still needs a
`semantic.dim_<x>` object for the catalogue to name — but writing a one-line
`SELECT * FROM live.dim_<x>` view by hand is pure boilerplate. The platform
materialises it instead: for every catalogue leaf dimension whose
`source.table` resolves to a `semantic.*` object with no operator-authored
semantic file, this emits `semantic.<x> AS SELECT * FROM live.<x>`.

Only leaf dimensions get a pass-through. Fact views (`v_*`) always do real work
(scenario unions, joins, filtering) and must be operator-authored — there is no
trivial `live.v_*` to pass through.
"""

from __future__ import annotations

from precis_mcp.engine.catalogue import Catalogue


def build_passthrough_views(
    catalogue: Catalogue, existing: set[str]
) -> list[tuple[str, str]]:
    """Return ``(view_name, sql_body)`` pass-throughs for catalogue leaf
    dimensions that have no operator-authored semantic file.

    ``existing`` is the set of semantic object stems already applied from
    ``instance/semantic/dims/*.sql``; those are skipped so an operator-authored
    view always wins. The live source is the same object name in the ``live``
    schema — the ``semantic.dim_x ← live.dim_x`` convention. ``sql_body`` is
    bare SQL (the caller wraps it in ``CREATE OR REPLACE VIEW``).
    """
    views: list[tuple[str, str]] = []
    seen: set[str] = set()
    for dim in catalogue.dimensions.values():
        if dim.source is None or not dim.source.table:
            continue
        ref = dim.source.table  # normalised to semantic.<stem> at catalogue load
        if not ref.startswith("semantic."):
            continue
        stem = ref.split(".", 1)[1]
        if stem in existing or stem in seen:
            continue
        seen.add(stem)
        views.append((stem, f"SELECT * FROM live.{stem}"))
    return views
