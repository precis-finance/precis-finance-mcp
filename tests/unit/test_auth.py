# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp.auth — permissions + ensure_user_directory.

JWT signing/verification + bcrypt helpers moved out of precis_mcp.auth
when Keycloak became the token issuer; their tests went with them.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from precis_mcp.auth import (
    DimensionScope,
    ScopeSpec,
    _compute_tool_scopes,
    _parse_scope,
    _resolve_profile_blocks,
    ensure_user_directory,
)


# ---------------------------------------------------------------------------
# _parse_scope tests (new allow/deny shape)
# ---------------------------------------------------------------------------


def test_parse_scope_none():
    assert _parse_scope(None) is None


def test_parse_scope_empty_axes():
    assert _parse_scope({}) is None
    assert _parse_scope({"dimensions": None, "domains": None}) is None


def test_parse_scope_dimensions_only():
    raw = {
        "dimensions": {"allow": {"cost_centre": ["CC-01"]}},
    }
    result = _parse_scope(raw)
    assert result is not None
    assert result.dimensions is not None
    assert result.dimensions.allow == {"cost_centre": ["CC-01"]}
    assert result.dimensions.deny is None
    assert result.domains is None


def test_parse_scope_domains_only():
    raw = {"domains": {"deny": ["payroll"]}}
    result = _parse_scope(raw)
    assert result is not None
    assert result.dimensions is None
    assert result.domains is not None
    assert result.domains.deny == ["payroll"]
    assert result.domains.allow is None


def test_parse_scope_allow_plus_deny():
    raw = {
        "dimensions": {
            "allow": {"cost_centre": ["CC-01", "CC-02"]},
            "deny":  {"cost_centre": ["CC-02"]},
        },
        "domains": {"allow": ["gl", "pnl"], "deny": ["payroll"]},
    }
    result = _parse_scope(raw)
    assert result is not None
    assert result.dimensions is not None
    assert result.dimensions.allow == {"cost_centre": ["CC-01", "CC-02"]}
    assert result.dimensions.deny == {"cost_centre": ["CC-02"]}
    assert result.domains is not None
    assert result.domains.allow == ["gl", "pnl"]
    assert result.domains.deny == ["payroll"]


# ---------------------------------------------------------------------------
# _compute_tool_scopes tests — input is dict[role, ScopeSpec|None]
# ---------------------------------------------------------------------------


def test_compute_tool_scopes_empty():
    role, tool_scopes = _compute_tool_scopes({})
    assert role == "analyst"
    assert tool_scopes == {}


def test_compute_tool_scopes_single_analyst():
    scope = ScopeSpec(
        dimensions=DimensionScope(allow={"cost_centre": ["CC-01"]}),
    )
    role, tool_scopes = _compute_tool_scopes({"analyst": scope})
    assert role == "analyst"
    assert tool_scopes["read"] is scope
    assert "write" not in tool_scopes
    assert "plan_manager" not in tool_scopes


def test_compute_tool_scopes_single_manager_unrestricted():
    role, tool_scopes = _compute_tool_scopes({"manager": None})
    assert role == "manager"
    assert tool_scopes["read"] is None
    assert tool_scopes["write"] is None
    assert tool_scopes["plan_manager"] is None


def test_compute_tool_scopes_planner_fills_read_and_write():
    scope = ScopeSpec(
        dimensions=DimensionScope(allow={"cost_centre": ["CC-01"]}),
    )
    role, tool_scopes = _compute_tool_scopes({"planner": scope})
    assert role == "planner"
    # Read falls up to planner block since no analyst block declared
    assert tool_scopes["read"] is scope
    assert tool_scopes["write"] is scope
    assert "plan_manager" not in tool_scopes


def test_compute_tool_scopes_distinct_levels_pick_lowest_covering():
    """Manager block + planner block, different scopes.

    Read (min_rank 0): pick lowest declared ≥ 0 → planner's scope.
    Write (min_rank 1): pick lowest declared ≥ 1 → planner's scope.
    plan_manager (min_rank 2): manager's scope.
    """
    mgr_scope = ScopeSpec(
        dimensions=DimensionScope(allow={"department": ["Software Engineering"]}),
    )
    planner_scope = ScopeSpec(
        dimensions=DimensionScope(allow={"cost_centre": ["CC-SENG-01"]}),
    )
    role, tool_scopes = _compute_tool_scopes(
        {"manager": mgr_scope, "planner": planner_scope},
    )
    assert role == "manager"
    assert tool_scopes["read"] is planner_scope
    assert tool_scopes["write"] is planner_scope
    assert tool_scopes["plan_manager"] is mgr_scope


def test_compute_tool_scopes_analyst_plus_manager():
    """Analyst block declared → read uses analyst; write/plan_manager use manager."""
    analyst_scope = ScopeSpec(
        dimensions=DimensionScope(allow={"cost_centre": ["CC-02"]}),
    )
    mgr_scope = ScopeSpec(
        dimensions=DimensionScope(allow={"cost_centre": ["CC-01"]}),
    )
    role, tool_scopes = _compute_tool_scopes(
        {"manager": mgr_scope, "analyst": analyst_scope},
    )
    assert role == "manager"
    assert tool_scopes["read"] is analyst_scope
    assert tool_scopes["write"] is mgr_scope
    assert tool_scopes["plan_manager"] is mgr_scope


# ---------------------------------------------------------------------------
# _resolve_profile_blocks tests — pattern matching + block extraction
# ---------------------------------------------------------------------------


def test_resolve_profile_blocks_literal_wins_over_category():
    pdef = {
        "scenarios": {
            "PLAN":     {"manager": {"dimensions": {"allow": {"d": ["PLAN"]}}}},
            "BUD-2026": {"planner": {"dimensions": {"allow": {"d": ["LIT"]}}}},
        },
    }
    blocks = _resolve_profile_blocks(pdef, "BUD-2026", "BUDGET")
    assert blocks is not None
    # Literal beats PLAN → only planner block used
    assert "planner" in blocks
    assert "manager" not in blocks


def test_resolve_profile_blocks_no_match():
    pdef = {"scenarios": {"ACTUAL": {"analyst": {}}}}
    assert _resolve_profile_blocks(pdef, "BUD-2026", "BUDGET") is None


def test_resolve_profile_blocks_unknown_role_dropped():
    pdef = {
        "scenarios": {
            "*": {
                "planner": {"dimensions": {"allow": {"d": ["X"]}}},
                "superuser": {"dimensions": {"allow": {"d": ["Y"]}}},
            },
        },
    }
    blocks = _resolve_profile_blocks(pdef, "ANY", "BUDGET")
    assert blocks is not None
    assert set(blocks.keys()) == {"planner"}


# ---------------------------------------------------------------------------
# User directory tests
# ---------------------------------------------------------------------------

def test_ensure_user_directory_creates_structure(tmp_path):
    with patch.dict(os.environ, {"USER_DATA_DIR": str(tmp_path)}):
        result = ensure_user_directory("dave")
    user_dir = Path(result)
    assert user_dir.is_dir()
    # Post-2026-04-30 registry layout: blobs/ holds file_id-keyed content,
    # sandbox_outputs/ is the per-run staging dir for sandbox writebacks.
    assert (user_dir / "blobs").is_dir()
    assert (user_dir / "sandbox_outputs").is_dir()
    assert user_dir == tmp_path / "dave"


def test_ensure_user_directory_idempotent(tmp_path):
    with patch.dict(os.environ, {"USER_DATA_DIR": str(tmp_path)}):
        first = ensure_user_directory("eve")
        second = ensure_user_directory("eve")
    assert first == second
