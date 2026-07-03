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
    build_diamond_check_sql,
    build_generated_edges_sql,
    build_hierarchy_sql,
    build_orphan_edges_sql,
    build_ragged_views,
    build_recursive_rollup_sql,
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

    def test_rollup_is_recursive_over_edges_view(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "WITH RECURSIVE" in sql
        assert "semantic.dim_cost_centre_org_structure_edges" in sql
        assert "SELECT DISTINCT" in sql

    def test_rollup_base_reads_semantic_leaf_source(self, cat):
        """Base case seeds every leaf → itself from the normalised semantic
        source (no dbt Jinja, no live.*)."""
        dim = cat.dimensions["org_structure"]
        sql = build_rollup_sql(dim, cat)
        assert "FROM semantic.dim_cost_centre" in sql
        assert "toString(cost_centre) AS node_id" in sql
        assert "{{" not in sql

    def test_project_rollup_reads_semantic_dim_project(self, cat):
        """client_portfolio leaf source resolves to semantic.dim_project."""
        dim = cat.dimensions["client_portfolio"]
        sql = build_rollup_sql(dim, cat)
        assert "FROM semantic.dim_project" in sql
        assert "semantic.dim_project_client_portfolio_edges" in sql
        assert "live.dim_project" not in sql


# ---------------------------------------------------------------------------
# Generated edges SQL (child→parent derived from the leaf table)
# ---------------------------------------------------------------------------

class TestGeneratedEdges:
    @pytest.fixture
    def cat(self):
        return load_catalogue(str(CATALOGUE_DIR))

    def test_adjacent_level_pairs_and_root_edge(self, cat):
        dim = cat.dimensions["org_structure"]
        sql = build_generated_edges_sql(dim, cat)
        # leaf-most first: cost_centre → department → division → all
        assert "toString(cost_centre) AS child_node_id" in sql
        assert "toString(department) AS parent_node_id" in sql
        assert "toString(department) AS child_node_id" in sql
        assert "toString(division) AS parent_node_id" in sql
        assert "toString(division) AS child_node_id" in sql
        assert "'all' AS parent_node_id" in sql
        assert "FROM semantic.dim_cost_centre" in sql

    def test_applies_node_prefix(self, cat):
        dim = cat.dimensions["client_portfolio"]
        sql = build_generated_edges_sql(dim, cat)
        assert "concat('CLI::', toString(" in sql   # client level node_prefix
        assert "'all' AS parent_node_id" in sql


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
    def test_returns_expected_views_for_all_ragged_dims(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        views = build_ragged_views(cat)
        names = {name for name, _ in views}
        # Every ragged dim (3 generated + 1 provided) emits edges + node master
        # + rollup = 4 × 3 = 12.
        assert len(views) == 12
        for stem in (
            "dim_cost_centre_org_structure",
            "dim_period_calendar",
            "dim_project_client_portfolio",
            "dim_cost_centre_solution_portfolio",
        ):
            assert stem in names
            assert f"{stem}_edges" in names
            assert f"{stem}_rollup" in names

    def test_view_bodies_are_sql(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        views = dict(build_ragged_views(cat))
        assert "WITH RECURSIVE" in views["dim_cost_centre_org_structure_rollup"]
        assert "all_nodes" in views["dim_cost_centre_org_structure"]
        assert "child_node_id" in views["dim_cost_centre_org_structure_edges"]

    def test_provided_hierarchy_generates_three_views(self, tmp_path):
        """A ragged hierarchy with source type='provided' generates its three
        semantic views (edges, node master, recursive rollup), with edges
        emitted before the rollup that reads them."""
        cat = load_catalogue(str(_provided_geo_catalogue(tmp_path)))
        names = [n for n, _ in build_ragged_views(cat)]
        assert "dim_region_geo_edges" in names
        assert "dim_region_geo" in names
        assert "dim_region_geo_rollup" in names
        assert names.index("dim_region_geo_edges") < names.index("dim_region_geo_rollup")


# ---------------------------------------------------------------------------
# Provided ragged hierarchy (operator node master + child→parent edges)
# ---------------------------------------------------------------------------

def _provided_geo_catalogue(tmp_path: Path) -> Path:
    (tmp_path / "cat").mkdir()
    return write_yml(tmp_path / "cat", "dimensions.yml", """
        dimensions:
          region:
            label: Region
            attributes:
              name: { label: Region Name }
            display_attribute: name
            source:
              table: dim_region
              key_column: region_id
              attribute_mapping:
                name: region_name
          geo:
            label: Geo
            ragged: true
            leaf_dimension: region
            source:
              type: provided
              node_table: geo_nodes
              edge_table: geo_edges
              child_column: child_id
              parent_column: parent_id
    """)


class TestProvidedHierarchy:
    @pytest.fixture
    def cat(self, tmp_path):
        return load_catalogue(str(_provided_geo_catalogue(tmp_path)))

    def test_edges_view_renames_to_platform_columns(self, cat):
        edges = dict(build_ragged_views(cat))["dim_region_geo_edges"]
        assert "child_id AS child_node_id" in edges
        assert "parent_id AS parent_node_id" in edges
        assert "FROM semantic.geo_edges" in edges

    def test_node_master_unions_operator_nodes_and_injected_leaves(self, cat):
        node = dict(build_ragged_views(cat))["dim_region_geo"]
        assert "FROM semantic.geo_nodes" in node          # operator nodes
        assert "UNION ALL" in node
        assert "FROM semantic.dim_region" in node          # injected leaves
        assert "'region' AS node_type" in node
        assert "region_name AS node_name" in node          # leaf display_attribute
        assert "node_name AS display_name" in node         # display derived from name

    def test_rollup_is_recursive_over_the_edges_view(self, cat):
        rollup = dict(build_ragged_views(cat))["dim_region_geo_rollup"]
        assert "WITH RECURSIVE" in rollup
        assert "INNER JOIN semantic.dim_region_geo_edges AS e" in rollup
        assert "SELECT DISTINCT" in rollup
        assert "toString(region_id) AS node_id" in rollup


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

        edges = build_generated_edges_sql(dim, cat)
        assert "toString(branch_id) AS child_node_id" in edges   # branch → region
        assert "concat('reg:', toString(region)) AS parent_node_id" in edges
        assert "concat('reg:', toString(region)) AS child_node_id" in edges  # region → all
        assert "'all' AS parent_node_id" in edges

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

        edges = build_generated_edges_sql(dim, cat)
        assert "toString(branch_id) AS child_node_id" in edges   # branch → region
        assert "toString(region) AS parent_node_id" in edges


# ---------------------------------------------------------------------------
# Recursive rollup SQL — edge-model closure shared by the provided and
# (post-homogenisation) generated paths.
# ---------------------------------------------------------------------------

class TestRecursiveRollupSql:
    def _sql(self):
        return build_recursive_rollup_sql(
            leaf_source="semantic.dim_cost_centre",
            leaf_key="cost_centre",
            edges_view="semantic.dim_cost_centre_solution_portfolio_edges",
        )

    def test_is_recursive(self):
        assert "WITH RECURSIVE rollup AS" in self._sql()

    def test_base_maps_each_leaf_to_itself(self):
        sql = self._sql()
        assert "toString(cost_centre) AS node_id" in sql
        assert "toString(cost_centre) AS cost_centre" in sql
        assert "FROM semantic.dim_cost_centre" in sql

    def test_recursive_term_climbs_child_to_parent(self):
        sql = self._sql()
        assert "e.parent_node_id AS node_id" in sql
        assert (
            "INNER JOIN semantic.dim_cost_centre_solution_portfolio_edges AS e "
            "ON e.child_node_id = r.node_id"
        ) in sql

    def test_cycle_guard_carries_and_checks_visited_path(self):
        sql = self._sql()
        assert "arrayPushBack(r._path, e.parent_node_id)" in sql
        assert "WHERE NOT has(r._path, e.parent_node_id)" in sql

    def test_dedups_diamonds_with_distinct(self):
        # A leaf reachable by more than one path must count once.
        assert "SELECT DISTINCT" in self._sql()

    def test_final_projection_is_node_id_and_leaf_key(self):
        sql = self._sql()
        tail = sql.rsplit("SELECT DISTINCT", 1)[1]
        assert "node_id" in tail
        assert "cost_centre" in tail
        assert "_path" not in tail  # the cycle-guard column is internal only

    def test_parameterised_by_leaf_and_edges(self):
        sql = build_recursive_rollup_sql(
            leaf_source="semantic.dim_branch",
            leaf_key="branch_id",
            edges_view="semantic.dim_branch_org_edges",
        )
        assert "toString(branch_id) AS node_id" in sql
        assert "FROM semantic.dim_branch" in sql
        assert "INNER JOIN semantic.dim_branch_org_edges AS e" in sql
        assert "cost_centre" not in sql


# ---------------------------------------------------------------------------
# Integrity checks — diamond (reconvergence) and orphan-edge detection
# ---------------------------------------------------------------------------

class TestIntegrityChecks:
    def test_diamond_check_counts_paths_without_dedup(self):
        sql = build_diamond_check_sql(
            leaf_source="semantic.dim_cost_centre",
            leaf_key="cost_centre",
            edges_view="semantic.dim_cost_centre_solution_portfolio_edges",
        )
        assert "WITH RECURSIVE" in sql
        assert "count() AS paths" in sql
        assert "GROUP BY node_id, cost_centre" in sql
        assert "HAVING count() > 1" in sql
        assert "SELECT DISTINCT" not in sql  # counts paths, must not dedup

    def test_orphan_check_flags_endpoints_absent_from_node_master(self):
        sql = build_orphan_edges_sql(
            node_master="semantic.dim_cost_centre_solution_portfolio",
            edges_view="semantic.dim_cost_centre_solution_portfolio_edges",
        )
        assert "child_node_id AS node_id, 'child'" in sql
        assert "parent_node_id AS node_id, 'parent'" in sql
        assert (
            "NOT IN (SELECT node_id FROM semantic.dim_cost_centre_solution_portfolio)"
        ) in sql
