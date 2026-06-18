# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp.auth — permissions + user directories.

Component-class subset: profile-driven `load_permissions` behaviour against
`FakePlatformDB`. The pure scope-parser tests live in
`tests/unit/test_auth.py`.
"""

from unittest.mock import patch

import pytest

from tests.fakes.fake_platform_db import FakePlatformDB

from precis_mcp.auth import (
    AuthError,
    UserPermissions,
    load_permissions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(db, user_id="sergio", is_admin=False):
    """Seed a user row in the fake DB.  Credentials live in Keycloak now;
    load_permissions reads only the users table + profile assignments."""
    db.users.append({
        "id": user_id,
        "entity_id": "ENT-001",
        "is_admin": is_admin,
    })


def _seed_profile(db, profile_id, definition):
    db.profiles.append({"profile_id": profile_id, "definition": definition})


def _assign_profile(db, user_id, profile_id):
    db.user_profile_assignments.append({
        "user_id": user_id,
        "profile_id": profile_id,
        "expires_at": None,
    })


# ---------------------------------------------------------------------------
# load_permissions tests (PostgreSQL-backed, profile-driven)
# ---------------------------------------------------------------------------


_FAKE_KINDS = {
    "BUD-2026":    "BUDGET",
    "FC-2026-Q1":  "FORECAST",
    "ACTUALS":     "ACTUAL",
}


def test_load_permissions_no_profile():
    db = FakePlatformDB()
    _seed_user(db, "sergio")
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", return_value=_FAKE_KINDS):
        perms = load_permissions("sergio")
    assert isinstance(perms, UserPermissions)
    assert perms.user_id == "sergio"
    assert perms.is_admin is False
    # No profile assigned → no per-scenario permissions
    assert perms.scenarios == {}


def test_load_permissions_admin():
    db = FakePlatformDB()
    _seed_user(db, "admin_user", is_admin=True)
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", return_value=_FAKE_KINDS):
        perms = load_permissions("admin_user")
    assert perms.is_admin is True
    assert perms.scenarios == {}


def test_load_scenario_kinds_reads_semantic_scenarios():
    from precis_mcp.auth import _load_scenario_kinds

    seen = {}

    def fake_query(sql):
        seen["sql"] = sql
        return [
            {"scenario_id": "ACTUALS", "kind": "ACTUAL"},
            {"scenario_id": "BUD-2026", "kind": "BUDGET"},
        ]

    with patch("precis_mcp.auth.query_clickhouse", side_effect=fake_query):
        result = _load_scenario_kinds()

    assert "FROM semantic.scenarios" in seen["sql"]
    assert result == {"ACTUALS": "ACTUAL", "BUD-2026": "BUDGET"}


def test_load_permissions_user_not_found():
    db = FakePlatformDB()
    with patch("precis_mcp.auth.query_platform", side_effect=db.query):
        with pytest.raises(AuthError, match="not found"):
            load_permissions("nonexistent_user")


def test_load_permissions_with_profile():
    """Profile with per-level scope on PLAN + analyst block on ACTUAL."""
    db = FakePlatformDB()
    _seed_user(db, "maria")
    _seed_profile(db, "eng-mgr", {
        "scenarios": {
            "PLAN": {
                "manager": {
                    "dimensions": {"allow": {"department": ["Software Engineering"]}},
                },
                "planner": {
                    "dimensions": {"allow": {"cost_centre": ["CC-SENG-01"]}},
                },
            },
            "ACTUAL": {
                "analyst": {
                    "dimensions": {"allow": {"department": ["Software Engineering"]}},
                },
            },
        },
    })
    _assign_profile(db, "maria", "eng-mgr")

    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", return_value=_FAKE_KINDS):
        perms = load_permissions("maria")

    assert set(perms.scenarios.keys()) == {"BUD-2026", "FC-2026-Q1", "ACTUALS"}

    # PLAN scenarios: manager block + planner block
    bud = perms.scenarios["BUD-2026"]
    assert bud.effective_role == "manager"
    assert "read" in bud.tool_scopes
    assert "write" in bud.tool_scopes
    assert "plan_manager" in bud.tool_scopes
    # read/write pick planner block (lowest ≥ min_rank)
    read_scope = bud.tool_scopes["read"]
    assert read_scope is not None
    assert read_scope.dimensions is not None
    assert read_scope.dimensions.allow == {"cost_centre": ["CC-SENG-01"]}
    # plan_manager picks manager block
    pm_scope = bud.tool_scopes["plan_manager"]
    assert pm_scope is not None
    assert pm_scope.dimensions is not None
    assert pm_scope.dimensions.allow == {"department": ["Software Engineering"]}

    # ACTUAL: analyst only — read only
    act = perms.scenarios["ACTUALS"]
    assert act.effective_role == "analyst"
    assert "read" in act.tool_scopes
    assert "write" not in act.tool_scopes
    assert "plan_manager" not in act.tool_scopes


def test_load_permissions_expired_assignment_ignored():
    from datetime import datetime, timedelta, timezone
    db = FakePlatformDB()
    _seed_user(db, "alice")
    _seed_profile(db, "p", {"scenarios": {"*": {"planner": {}}}})
    db.user_profile_assignments.append({
        "user_id": "alice",
        "profile_id": "p",
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    })
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", return_value=_FAKE_KINDS):
        perms = load_permissions("alice")
    assert perms.scenarios == {}
