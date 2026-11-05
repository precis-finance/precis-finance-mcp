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
    AccountDisabledError,
    AuthContext,
    AuthError,
    UserPermissions,
    load_permissions,
    refresh_permissions,
    set_auth_context,
    clear_auth_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(db, user_id="sergio", is_admin=False):
    """Seed a user row in the fake DB.  Credentials live in Keycloak now;
    load_permissions reads only the users table + profile assignments."""
    db.users.append({
        "id": user_id,
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


@pytest.mark.parametrize("backend", ["clickhouse", "databricks"])
def test_load_scenario_kinds_reads_semantic_scenarios(ch_client, backend):
    from precis_mcp.auth import _load_scenario_kinds
    from precis_mcp.analytical.sql import SqlDialect
    from tests.fakes.fake_clickhouse import FakeQueryResult
    ch_client.dialect = SqlDialect(backend, "customer")
    ch_client.set_response("FROM ", FakeQueryResult(
        column_names=["scenario_id", "alias", "name", "kind"],
        result_rows=[("ACTUALS", "actuals", "Actuals", "ACTUAL"),
                     ("BUD-2026", "budget", "Budget", "BUDGET")]))
    with patch("precis_mcp.auth.get_analytical_client", return_value=ch_client):
        result = _load_scenario_kinds()
        assert _load_scenario_kinds() == result
    assert f"FROM {ch_client.dialect.table('semantic.scenarios')}" in ch_client.queries[-1][0]
    assert ch_client.close_calls == 1
    assert len(ch_client.queries) == 1
    assert result == {"ACTUALS": "ACTUAL", "BUD-2026": "BUDGET"}


def test_load_permissions_user_not_found():
    db = FakePlatformDB()
    with patch("precis_mcp.auth.query_platform", side_effect=db.query):
        with pytest.raises(AuthError, match="not found"):
            load_permissions("nonexistent_user")


def test_cached_scenarios_do_not_cache_profile_grants_or_disabled_accounts(ch_client):
    from tests.fakes.fake_clickhouse import FakeQueryResult

    db = FakePlatformDB()
    _seed_user(db, "actor")
    _seed_profile(db, "all", {"scenarios": {"*": {"analyst": {}}}})
    _assign_profile(db, "actor", "all")
    ch_client.set_response("FROM semantic.scenarios", FakeQueryResult(
        column_names=["scenario_id", "alias", "name", "kind"],
        result_rows=[("ACTUALS", "actuals", "Actuals", "ACTUAL")],
    ))
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth.get_analytical_client", return_value=ch_client):
        assert "ACTUALS" in load_permissions("actor").scenarios
        db.user_profile_assignments.clear()
        assert load_permissions("actor").scenarios == {}
        _assign_profile(db, "actor", "all")
        assert "ACTUALS" in load_permissions("actor").scenarios
        db.users[0]["is_disabled"] = True
        with pytest.raises(AccountDisabledError):
            load_permissions("actor")
    assert len(ch_client.queries) == 1


def test_registry_refresh_failure_clears_previously_loaded_permissions(ch_client, monkeypatch):
    from precis_mcp.engine import scenario_registry
    from tests.fakes.fake_clickhouse import FakeQueryResult

    db = FakePlatformDB()
    _seed_user(db, "actor", is_admin=True)
    _seed_profile(db, "all", {"scenarios": {"*": {"analyst": {}}}})
    _assign_profile(db, "actor", "all")
    ch_client.set_response("FROM semantic.scenarios", FakeQueryResult(
        column_names=["scenario_id", "alias", "name", "kind"],
        result_rows=[("ACTUALS", "actuals", "Actuals", "ACTUAL")],
    ))
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth.get_analytical_client", side_effect=[
             ch_client, RuntimeError("Warehouse unavailable"),
         ]):
        permissions = load_permissions("actor")
        set_auth_context(AuthContext("actor", permissions))
        try:
            monkeypatch.setattr(scenario_registry, "_REGISTRY_TTL_SECONDS", 0)
            with pytest.raises(RuntimeError, match="Warehouse unavailable"):
                refresh_permissions()
            assert permissions.scenarios == {}
            assert permissions.is_admin is False
        finally:
            clear_auth_context()


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


@pytest.mark.asyncio
async def test_refresh_permissions_propagates_from_worker_to_shared_snapshot():
    import asyncio

    db = FakePlatformDB()
    _seed_user(db, "actor")
    _seed_profile(db, "all-plan", {"scenarios": {"PLAN": {"manager": {}}}})
    _assign_profile(db, "actor", "all-plan")
    kinds = {"BUD-2026": "BUDGET"}
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", side_effect=lambda: dict(kinds)):
        snapshot = load_permissions("actor")
        ctx = AuthContext("actor", snapshot, conversation_id="thread")
        set_auth_context(ctx)
        try:
            kinds["FC-NEW"] = "FORECAST"
            await asyncio.to_thread(refresh_permissions)
            assert ctx.permissions is snapshot
            assert "FC-NEW" in snapshot.scenarios
            assert ctx.conversation_id == "thread"
        finally:
            clear_auth_context()


@pytest.mark.parametrize("failure", [AccountDisabledError("disabled"), RuntimeError("DB unavailable")])
def test_refresh_failure_clears_old_grants(failure):
    from tests.factories.auth import make_permissions, make_scenario_permissions

    snapshot = make_permissions(
        "actor", is_admin=True, scenarios={"BUD-2026": make_scenario_permissions()},
    )
    set_auth_context(AuthContext("actor", snapshot))
    try:
        with patch("precis_mcp.auth.load_permissions", side_effect=failure):
            with pytest.raises(type(failure)):
                refresh_permissions()
        assert snapshot.scenarios == {}
        assert snapshot.is_admin is False
    finally:
        clear_auth_context()


def test_refresh_without_auth_context_is_dev_noop():
    clear_auth_context()
    with patch("precis_mcp.auth.load_permissions") as load:
        assert refresh_permissions() is None
    load.assert_not_called()


def test_refresh_revalidates_profile_assignment():
    db = FakePlatformDB()
    _seed_user(db, "actor")
    _seed_profile(db, "all", {"scenarios": {"*": {"manager": {}}}})
    _assign_profile(db, "actor", "all")
    with patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds", return_value=_FAKE_KINDS):
        snapshot = load_permissions("actor")
        assert snapshot.scenarios
        set_auth_context(AuthContext("actor", snapshot))
        try:
            db.user_profile_assignments.clear()
            refresh_permissions()
            assert snapshot.scenarios == {}
        finally:
            clear_auth_context()
