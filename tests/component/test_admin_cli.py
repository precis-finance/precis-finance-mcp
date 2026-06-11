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
