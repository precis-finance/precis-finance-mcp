# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/ragged_views.py — catalogue-derived ragged
hierarchy view generation (the in-memory successor to the retired
scripts/generate_hierarchy_views.py)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis_mcp.engine.catalogue import load_catalogue
from precis_mcp.engine.ragged_views import (
    build_hierarchy_sql,
    build_ragged_views,
    build_rollup_sql,
    _resolve_level_info,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = PROJECT_ROOT / "instance" / "catalogue"


def write_yml(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content))
    return tmp_path


# ---------------------------------------------------------------------------
# Rollup SQL generation
# ---------------------------------------------------------------------------

class TestRollupSql:
    @pytest.fixture
    def cat(self):
        return load_catalogue(str(CATALOGUE_DIR))

    def test_rollup_reads_semantic_source(self, cat):
        """Reads the leaf's normalised semantic source — no dbt Jinja, no live.*."""
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "FROM semantic.dim_cost_centre" in sql
        assert "{{" not in sql  # no Jinja survived the dbt-era port

    def test_rollup_has_all_union_blocks(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        # 4 blocks: division, department, leaf (self), root → 3 UNION ALLs
        assert sql.count("UNION ALL") == 3

    def test_rollup_division_no_prefix(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "toString(division) AS node_id" in sql

    def test_rollup_department_no_prefix(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "toString(department) AS node_id" in sql

    def test_rollup_leaf_self_reference(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "toString(cost_centre) AS node_id" in sql

    def test_rollup_root_node(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "'all'" in sql

    def test_project_rollup_reads_semantic_dim_project(self, cat):
        """client_portfolio leaf source resolves to semantic.dim_project."""
        dim = cat.dimensions["client_portfolio"]
        sql = build_rollup_sql(dim, cat)
        assert "FROM semantic.dim_project" in sql
        assert "live.dim_project" not in sql


# ---------------------------------------------------------------------------
# Hierarchy SQL generation
# ---------------------------------------------------------------------------

class TestHierarchySql:
    @pytest.fixture
    def cat(self):
        return load_catalogue(str(CATALOGUE_DIR))

    def test_hierarchy_reads_semantic_source(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "FROM semantic.dim_cost_centre" in sql
        assert "{{" not in sql

    def test_hierarchy_has_root_node(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "— All Cost Centres —" in sql
        assert "'all'" in sql

    def test_hierarchy_level_numbers(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "0                      AS level" in sql

    def test_hierarchy_display_prefixes(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "[D] " in sql
        assert "[BU] " in sql
        assert "[CC] " in sql

    def test_hierarchy_node_types(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "'division'" in sql
        assert "'department'" in sql
        assert "'cost_centre'" in sql
        assert "'all'" in sql

    def test_hierarchy_leaf_display_attribute(self, cat):
        """Leaf level with display attribute produces concat(id, ' · ', name)."""
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "cost_centre_name" in sql
        assert "' · '" in sql

    def test_hierarchy_sort_keys(self, cat):
        """Sort keys use '|'-separated ancestor columns wrapped in toString()."""
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "concat(toString(division), '|')" in sql
        assert "toString(division), '|', toString(department), '|')" in sql

    def test_hierarchy_cte_names(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        assert "divisions AS" in sql
        assert "departments AS" in sql
        assert "cost_centres AS" in sql
        assert "all_nodes AS" in sql

    def test_hierarchy_distinct_on_non_leaf(self, cat):
        """Non-leaf CTEs use SELECT DISTINCT, leaf does not."""
        dim = cat.dimensions["org_structure"]
        sql = build_hierarchy_sql(dim, cat)
        lines = sql.split("\n")
        in_divisions = False
        in_cost_centres = False
        for line in lines:
            if "divisions AS" in line:
                in_divisions = True
            if in_divisions and "SELECT" in line:
                assert "DISTINCT" in line
                in_divisions = False
            if "cost_centres AS" in line:
                in_cost_centres = True
            if in_cost_centres and "SELECT" in line:
                assert "DISTINCT" not in line
                in_cost_centres = False

    def test_project_hierarchy_joins_semantic_dim_client(self, cat):
        """The client level joins its own (normalised semantic) source."""
        dim = cat.dimensions["client_portfolio"]
        sql = build_hierarchy_sql(dim, cat)
        assert "JOIN semantic.dim_client" in sql
        assert "{{" not in sql


# ---------------------------------------------------------------------------
# Level info resolution
# ---------------------------------------------------------------------------

class TestResolveLevelInfo:
    def test_level_info_root_to_leaf(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["org_structure"]
        levels = _resolve_level_info(dim, cat)
        assert len(levels) == 3
        assert levels[0]["dimension_key"] == "division"
        assert levels[0]["column"] == "division"
        assert levels[0]["node_prefix"] == ""
        assert levels[1]["dimension_key"] == "department"
        assert levels[1]["column"] == "department"
        assert levels[1]["node_prefix"] == ""
        assert levels[2]["dimension_key"] == "cost_centre"
        assert levels[2]["column"] == "cost_centre"
        assert levels[2]["is_leaf"] is True
        assert levels[2]["node_prefix"] == ""


# ---------------------------------------------------------------------------
# build_ragged_views — the dispatcher used by semantic_runner
# ---------------------------------------------------------------------------

class TestBuildRaggedViews:
    def test_returns_two_views_per_generated_dimension(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        views = build_ragged_views(cat)
        names = {name for name, _ in views}
        # cost_centre/org_structure + period/calendar + project/client_portfolio
        # = 3 dimensions × 2 views = 6
        assert len(views) == 6
        assert "dim_cost_centre_org_structure_rollup" in names
        assert "dim_cost_centre_org_structure" in names
        assert "dim_period_calendar_rollup" in names
        assert "dim_period_calendar" in names
        assert "dim_project_client_portfolio_rollup" in names
        assert "dim_project_client_portfolio" in names

    def test_view_bodies_are_sql(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        views = dict(build_ragged_views(cat))
        assert "UNION ALL" in views["dim_cost_centre_org_structure_rollup"]
        assert "all_nodes" in views["dim_cost_centre_org_structure"]

    def test_skips_provided_hierarchies(self, tmp_path):
        """A ragged hierarchy with source type='provided' generates nothing."""
        (tmp_path / "cat").mkdir()
        cat_dir = write_yml(tmp_path / "cat", "dimensions.yml", """
            dimensions:
              region:
                label: Region
                source:
                  table: regions
                  key_column: region_id
              geo:
                label: Geo
                ragged: true
                leaf_dimension: region
                root_label: "— All Regions —"
                levels:
                  - dimension: region
                source:
                  type: provided
                  table: semantic.geo_rollup
        """)
        cat = load_catalogue(str(cat_dir))
        assert build_ragged_views(cat) == []


# ---------------------------------------------------------------------------
# Custom dimension (not cost_centre)
# ---------------------------------------------------------------------------

class TestCustomDimension:
    def test_two_level_hierarchy_with_node_prefix(self, tmp_path):
        """A 2-level hierarchy with explicit node_prefix generates concat()
        for prefixed levels."""
        (tmp_path / "cat").mkdir()
        cat_dir = write_yml(tmp_path / "cat", "dimensions.yml", """
            dimensions:
              branch:
                label: Branch
                attributes:
                  name: { label: Branch Name }
                display_attribute: name
                source:
                  table: branches
                  key_column: branch_id
                  attribute_mapping:
                    name: branch_name
                parents:
                  region:
                    source_column: region
              region:
                label: Region
                derived_from:
                  dimension: branch
                  source_column: region
              org:
                label: Organisation
                ragged: true
                leaf_dimension: branch
                root_label: "— All Branches —"
                levels:
                  - dimension: region
                    display_prefix: "[R] "
                    node_prefix: "reg:"
                  - dimension: branch
                    display_prefix: "[B] "
                source:
                  type: generated
        """)
        cat = load_catalogue(str(cat_dir))
        dim = cat.dimensions["org"]

        rollup = build_rollup_sql(dim, cat)
        assert "concat('reg:', toString(region)) AS node_id" in rollup
        assert "toString(branch_id) AS node_id" in rollup
        assert "'all'" in rollup
        # region, leaf, root = 3 blocks = 2 UNION ALLs
        assert rollup.count("UNION ALL") == 2

        hierarchy = build_hierarchy_sql(dim, cat)
        assert "[R] " in hierarchy
        assert "[B] " in hierarchy
        assert "— All Branches —" in hierarchy
        assert "branch_name" in hierarchy
        assert "'region'" in hierarchy   # node_type
        assert "'branch'" in hierarchy   # node_type

    def test_two_level_hierarchy_no_prefix(self, tmp_path):
        """A 2-level hierarchy with no node_prefix uses raw column values."""
        (tmp_path / "cat").mkdir()
        cat_dir = write_yml(tmp_path / "cat", "dimensions.yml", """
            dimensions:
              branch:
                label: Branch
                source:
                  table: branches
                  key_column: branch_id
                parents:
                  region:
                    source_column: region
              region:
                label: Region
                derived_from:
                  dimension: branch
                  source_column: region
              org:
                label: Organisation
                ragged: true
                leaf_dimension: branch
                root_label: "— All —"
                levels:
                  - dimension: region
                  - dimension: branch
                source:
                  type: generated
        """)
        cat = load_catalogue(str(cat_dir))
        dim = cat.dimensions["org"]

        rollup = build_rollup_sql(dim, cat)
        assert "toString(region) AS node_id" in rollup
        assert "toString(branch_id) AS node_id" in rollup
