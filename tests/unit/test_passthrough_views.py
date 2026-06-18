# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/passthrough_views.py — auto-generated trivial
pass-through semantic views for catalogue leaf dimensions with no operator file."""
from __future__ import annotations

import textwrap
from pathlib import Path

from precis_mcp.engine.catalogue import load_catalogue
from precis_mcp.engine.passthrough_views import build_passthrough_views


def _load(tmp_path: Path, body: str):
    (tmp_path / "dimensions.yml").write_text(textwrap.dedent(body))
    return load_catalogue(str(tmp_path))


_DIMS = """
    dimensions:
      cost_centre:
        label: CC
        source:
          table: dim_cost_centre
          key_column: cost_centre
      period:
        label: Period
        source:
          table: dim_period
          key_column: period
      department:
        label: Dept
        derived_from:
          dimension: cost_centre
          source_column: department
"""


def test_passthrough_for_dims_without_a_file(tmp_path):
    cat = _load(tmp_path, _DIMS)
    # No operator semantic files exist → both leaf dims get a pass-through.
    views = dict(build_passthrough_views(cat, existing=set()))
    assert views["dim_cost_centre"] == "SELECT * FROM live.dim_cost_centre"
    assert views["dim_period"] == "SELECT * FROM live.dim_period"


def test_operator_file_wins(tmp_path):
    cat = _load(tmp_path, _DIMS)
    # dim_cost_centre already applied from a file → only dim_period is generated.
    views = dict(build_passthrough_views(cat, existing={"dim_cost_centre"}))
    assert "dim_cost_centre" not in views
    assert views["dim_period"] == "SELECT * FROM live.dim_period"


def test_derived_dimensions_get_no_passthrough(tmp_path):
    cat = _load(tmp_path, _DIMS)
    names = {n for n, _ in build_passthrough_views(cat, existing=set())}
    # `department` is derived (no own source) — never gets a pass-through.
    assert "dim_department" not in names
    assert "department" not in names


def test_live_source_stem_mirrors_semantic_object(tmp_path):
    # The normalized source is semantic.dim_period; the pass-through reads the
    # same object name in the live schema.
    cat = _load(tmp_path, _DIMS)
    assert cat.dimensions["period"].source.table == "semantic.dim_period"
    views = dict(build_passthrough_views(cat, existing=set()))
    assert views["dim_period"] == "SELECT * FROM live.dim_period"
