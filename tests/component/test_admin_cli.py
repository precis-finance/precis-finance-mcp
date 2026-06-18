# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Component tests for the open admin CLI (precis_mcp/admin_cli.py).

The CLI shares all logic with the admin routes via ``admin_ops``; these tests
exercise the CLI dispatch + the ops against the in-memory FakePlatformDB. The
ops lazy-import db/keycloak/auth from the source modules, so the patches target
those sources (refactor-proof).
"""
from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from precis_mcp import admin_cli, admin_ops
from tests.fakes.fake_platform_db import FakePlatformDB


@contextlib.contextmanager
def _patched(db, kc=None):
    kc = kc if kc is not None else MagicMock()
    with patch("precis_mcp.db.query_platform", side_effect=db.query), \
         patch("precis_mcp.db.execute_platform", side_effect=db.execute), \
         patch("precis_mcp.auth.ensure_user_directory"), \
         patch("precis_mcp.keycloak_admin.KeycloakAdmin", return_value=kc):
        yield kc


def _seed_profile(db, profile_id="p1", name="P"):
    db.profiles.append({
        "profile_id": profile_id, "name": name, "description": "",
        "definition": {"scenarios": {"*": {"analyst": {}}}}, "updated_by": "seed",
    })


# --- bootstrap / user creation ---------------------------------------------


def test_create_admin_mode_b_provisions_keycloak(capsys):
    db = FakePlatformDB()
    with _patched(db) as kc:
        admin_cli.main(["create-admin", "--id", "boss", "--password", "longpassword123"])
    row = next(u for u in db.users if u["id"] == "boss")
    assert row["is_admin"] is True
    kc.create_user.assert_called_once()


def test_create_admin_mode_c_skips_keycloak(capsys):
    db = FakePlatformDB()
    with _patched(db) as kc:
        admin_cli.main(["create-admin", "--id", "boss", "--no-keycloak"])
    row = next(u for u in db.users if u["id"] == "boss")
    assert row["is_admin"] is True
    kc.create_user.assert_not_called()


def test_create_admin_autogenerates_password(capsys):
    db = FakePlatformDB()
    with _patched(db) as kc:
        admin_cli.main(["create-admin", "--id", "boss"])
    assert "temporary password" in capsys.readouterr().out
    kc.create_user.assert_called_once()


def test_create_user_with_external_id(capsys):
    db = FakePlatformDB()
    with _patched(db) as kc:
        admin_cli.main([
            "create-user", "--id", "jane", "--password", "longpassword123",
            "--external-id", "GUID-123",
        ])
    row = next(u for u in db.users if u["id"] == "jane")
    assert row["external_id"] == "GUID-123"
    kc.create_user.assert_called_once()


def test_create_user_weak_password_exits_validation(capsys):
    db = FakePlatformDB()
    with _patched(db):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["create-user", "--id", "u2", "--password", "short"])
    assert exc.value.code == admin_ops.ADMIN_EXIT_CODE[admin_ops.AdminValidationError]


def test_create_user_duplicate_exits_conflict(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("dup"))
    with _patched(db):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["create-user", "--id", "dup", "--password", "longpassword123"])
    assert exc.value.code == admin_ops.ADMIN_EXIT_CODE[admin_ops.ConflictError]


def test_create_user_unwritable_data_dir_aborts_before_any_mutation(capsys):
    # An unwritable USER_DATA_DIR must fail the create cleanly (ProvisioningError,
    # not a traceback) before the Keycloak account or the users row exists —
    # the directory is created first because it is the only harmless leftover.
    db = FakePlatformDB()
    with _patched(db) as kc, \
         patch("precis_mcp.auth.ensure_user_directory",
               side_effect=PermissionError("read-only file system")):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["create-user", "--id", "u3", "--password", "longpassword123"])
    assert exc.value.code == admin_ops.ADMIN_EXIT_CODE[admin_ops.ProvisioningError]
    assert not any(u["id"] == "u3" for u in db.users)
    kc.create_user.assert_not_called()


# --- assignment ------------------------------------------------------------


def test_assign_then_revoke(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    _seed_profile(db, "p1")
    with _patched(db):
        admin_cli.main(["assign", "--user", "u1", "--profile", "p1"])
        assert db.user_profile_assignments[0]["profile_id"] == "p1"
        assert db.user_profile_assignments[0]["source"] == "admin_cli"
        admin_cli.main(["revoke", "--user", "u1"])
        assert db.user_profile_assignments == []


def test_assign_unknown_profile_exits_not_found(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    with _patched(db):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["assign", "--user", "u1", "--profile", "ghost"])
    assert exc.value.code == admin_ops.ADMIN_EXIT_CODE[admin_ops.NotFoundError]


# --- profiles --------------------------------------------------------------


def test_check_auth_ok(capsys):
    with patch("precis_mcp.oidc.check_token_contract", return_value=[]):
        admin_cli.main(["check-auth", "--no-fetch"])
    assert "OK" in capsys.readouterr().out


def test_check_auth_problems_exit_1(capsys):
    with patch("precis_mcp.oidc.check_token_contract", return_value=["no /mcp audience"]):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["check-auth"])
    assert exc.value.code == 1


def test_profile_create_from_stdin(capsys, monkeypatch):
    db = FakePlatformDB()
    yaml_doc = "profile_id: p2\nname: P Two\nscenarios:\n  '*':\n    analyst: {}\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(yaml_doc))
    with _patched(db):
        admin_cli.main(["profile", "create", "--file", "-"])
    assert any(p["profile_id"] == "p2" for p in db.profiles)
    assert db.profile_audit[0]["change_kind"] == "create"


# --- audit trail: privilege-grant operations -------------------------------


def test_create_user_writes_audit(capsys):
    db = FakePlatformDB()
    with _patched(db):
        admin_cli.main(["create-user", "--id", "jane", "--password", "longpassword123"])
    row = next(r for r in db.security_audit_log if r["event_type"] == "user_created")
    assert row["target_user_id"] == "jane"
    assert row["details"]["is_admin"] is False
    assert row["details"]["keycloak"] is True


def test_set_admin_writes_grant_then_revoke_audit(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    with _patched(db):
        admin_cli.main(["set-admin", "--id", "u1"])
        admin_cli.main(["set-admin", "--id", "u1", "--off"])
    events = [r["event_type"] for r in db.security_audit_log]
    assert events == ["admin_granted", "admin_revoked"]
    assert all(r["target_user_id"] == "u1" for r in db.security_audit_log)


def test_disable_and_reset_password_write_audit(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    with _patched(db):
        admin_cli.main(["disable-user", "--id", "u1"])
        admin_cli.main(["reset-password", "--id", "u1", "--password", "longpassword123"])
    assert [r["event_type"] for r in db.security_audit_log] == [
        "user_disabled", "password_reset",
    ]


def test_reset_password_mode_c_no_keycloak_is_guidance_only(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    with _patched(db) as kc:
        admin_cli.main(["reset-password", "--id", "u1", "--no-keycloak"])
    out = capsys.readouterr().out
    assert "mode C" in out
    kc.reset_password.assert_not_called()       # no opaque Keycloak attempt
    assert db.security_audit_log == []          # nothing reset → nothing audited


def test_assign_and_revoke_write_audit(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    _seed_profile(db, "p1")
    with _patched(db):
        admin_cli.main(["assign", "--user", "u1", "--profile", "p1"])
        admin_cli.main(["revoke", "--user", "u1"])
    assert [r["event_type"] for r in db.security_audit_log] == [
        "profile_assigned", "profile_revoked",
    ]
    assigned = db.security_audit_log[0]
    assert assigned["target_user_id"] == "u1"
    assert assigned["details"] == {"profile_id": "p1", "source": "admin_cli"}


# --- audit reader ---------------------------------------------------------


def test_audit_reader_filters_by_event(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    _seed_profile(db, "p1")
    with _patched(db):
        admin_cli.main(["assign", "--user", "u1", "--profile", "p1"])
        admin_cli.main(["set-admin", "--id", "u1"])
        capsys.readouterr()  # drop the command-confirmation output
        admin_cli.main(["audit", "--event", "profile_assigned"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "profile_assigned"
    assert rows[0]["target_user_id"] == "u1"


def test_audit_reader_newest_first_and_limit(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    with _patched(db):
        admin_cli.main(["set-admin", "--id", "u1"])
        admin_cli.main(["set-admin", "--id", "u1", "--off"])
        capsys.readouterr()
        admin_cli.main(["audit", "--limit", "1"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "admin_revoked"  # newest first


# --- effective-access review ----------------------------------------------


def test_show_access_user_resolves_effective_scopes(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    _seed_profile(db, "p1")  # scenarios: {"*": {"analyst": {}}}
    with _patched(db), \
         patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds",
               return_value={"budget-2026": "BUDGET"}):
        admin_cli.main(["assign", "--user", "u1", "--profile", "p1"])
        capsys.readouterr()
        admin_cli.main(["show-access", "--user", "u1"])
    out = json.loads(capsys.readouterr().out)
    assert out["user_id"] == "u1"
    assert out["is_admin"] is False
    assert out["scenarios"]["budget-2026"]["effective_role"] == "analyst"


def test_show_access_scenario_reverse_lookup(capsys):
    db = FakePlatformDB()
    db.users.append(db._default_user("u1"))
    db.users.append(db._default_user("u2"))  # no profile → no access
    _seed_profile(db, "p1")
    with _patched(db), \
         patch("precis_mcp.auth.query_platform", side_effect=db.query), \
         patch("precis_mcp.auth._load_scenario_kinds",
               return_value={"budget-2026": "BUDGET"}):
        admin_cli.main(["assign", "--user", "u1", "--profile", "p1"])
        capsys.readouterr()
        admin_cli.main(["show-access", "--scenario", "budget-2026"])
    out = json.loads(capsys.readouterr().out)
    assert out["scenario"] == "budget-2026"
    assert {u["user_id"] for u in out["users"]} == {"u1"}
