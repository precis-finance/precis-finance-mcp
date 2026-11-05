# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Scoped provisioning protocol, shared by admin HTTP routes and CLI."""
import json

import httpx
import pytest

from precis_mcp.keycloak_admin import KeycloakAdmin, KeycloakAdminError
from tests.fakes.fake_keycloak import FakeKeycloak


@pytest.fixture
def provisioner(monkeypatch):
    fake = FakeKeycloak()
    client = fake.client()
    monkeypatch.setenv("KC_PROVISIONING_CLIENT_SECRET", "test-provision-secret")
    monkeypatch.setenv("KC_BOOTSTRAP_ADMIN_PASSWORD", "operator-secret-never-used")
    monkeypatch.setenv("KC_REALM", "custom-realm")
    monkeypatch.setenv("KC_BASE_URL_INTERNAL", "http://keycloak.invalid/auth")
    monkeypatch.setattr("precis_mcp.keycloak_admin.httpx.Client", lambda **kw: client)
    yield KeycloakAdmin(), fake
    client.close()


def test_grouped_lifecycle_uses_realm_local_client_credentials(provisioner):
    kc, fake = provisioner
    uid = kc.create_user(username="new-user", precis_user_id="new-user", password="a-temporary-password")
    kc.reset_password(username="new-user", password="another-password")
    kc.clear_required_actions("new-user")
    kc.set_enabled(username="new-user", enabled=False)
    kc.delete_user("new-user")
    assert uid == "user-uuid"
    assert fake.tokens == [{"grant_type": ["client_credentials"],
        "client_id": ["precis-user-provisioner"], "client_secret": ["test-provision-secret"]}]
    assert fake.requests[0].url.path == "/auth/realms/custom-realm/protocol/openid-connect/token"
    assert all(r.url.path.startswith("/auth/admin/realms/custom-realm/") for r in fake.requests[1:])
    user = fake.users["new-user"]
    assert user["groups"] == ["/precis-application-users"]
    assert user["attributes"] == {"precis_user_id": ["new-user"]}
    assert user["requiredActions"] == ["UPDATE_PASSWORD"]
    assert user["credentials"][0]["temporary"] is True
    reset = next(r for r in fake.requests if r.url.path.endswith("/reset-password"))
    assert json.loads(reset.content)["temporary"] is True
    lookups = [r for r in fake.requests if r.method == "GET"]
    assert all(r.url.params["exact"] == "true" for r in lookups)


def test_missing_secret_never_uses_available_operator_credential(provisioner, monkeypatch):
    kc, fake = provisioner
    monkeypatch.delenv("KC_PROVISIONING_CLIENT_SECRET")
    monkeypatch.delenv("KC_PROVISIONING_CLIENT_SECRET_FILE", raising=False)
    with pytest.raises(KeycloakAdminError, match="KC_PROVISIONING_CLIENT_SECRET is not set"):
        kc.find_user_id("someone")
    assert fake.requests == []


def test_secret_file_and_plain_precedence(provisioner, monkeypatch, tmp_path):
    kc, fake = provisioner
    path = tmp_path / "secret"
    path.write_text("file-secret\n")
    monkeypatch.setenv("KC_PROVISIONING_CLIENT_SECRET_FILE", str(path))
    kc.find_user_id("someone")
    assert fake.tokens[-1]["client_secret"] == ["test-provision-secret"]
    monkeypatch.delenv("KC_PROVISIONING_CLIENT_SECRET")
    kc._token = None
    kc.find_user_id("someone")
    assert fake.tokens[-1]["client_secret"] == ["file-secret"]


def test_unreadable_secret_file_is_actionable(provisioner, monkeypatch, tmp_path):
    kc, fake = provisioner
    monkeypatch.delenv("KC_PROVISIONING_CLIENT_SECRET")
    monkeypatch.setenv("KC_PROVISIONING_CLIENT_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(KeycloakAdminError, match="not readable"):
        kc.find_user_id("someone")
    assert fake.requests == []


@pytest.mark.parametrize("statuses,token_count,request_count", [
    ([401, 200], 2, 2), ([401, 401], 2, 2), ([403], 1, 1), ([503], 1, 1),
])
def test_only_401_refreshes_once(provisioner, statuses, token_count, request_count):
    kc, fake = provisioner
    fake.responses = [httpx.Response(s, json=[] if s == 200 else {"error": "sensitive-upstream"}) for s in statuses]
    if statuses[-1] == 200:
        assert kc.find_user_id("someone") is None
    else:
        with pytest.raises(KeycloakAdminError) as exc:
            kc.find_user_id("someone")
        assert "sensitive-upstream" not in str(exc.value)
    assert len(fake.tokens) == token_count
    assert len([r for r in fake.requests if not r.url.path.endswith("/token")]) == request_count


def test_ambiguous_creation_failure_is_not_replayed(provisioner):
    kc, fake = provisioner
    fake.responses = [httpx.ReadTimeout("upstream-sensitive-body")]
    with pytest.raises(KeycloakAdminError, match="check connectivity"):
        kc.create_user(username="u", precis_user_id="u", password="temporary-password")
    assert len([r for r in fake.requests if r.url.path.endswith("/users")]) == 1


def test_duplicate_creation_is_a_conflict(provisioner):
    kc, fake = provisioner
    fake.responses = [httpx.Response(409)]
    with pytest.raises(KeycloakAdminError, match="already exists"):
        kc.create_user(username="u", precis_user_id="u", password="temporary-password")


def test_rejected_credentials_are_sanitized_and_never_fall_back(provisioner):
    kc, fake = provisioner
    fake.login_responses = [httpx.Response(401, text="sensitive-upstream-secret")]
    with pytest.raises(KeycloakAdminError, match="authentication failed \\(401\\)") as exc:
        kc.find_user_id("u")
    assert "sensitive" not in str(exc.value)
    assert len(fake.tokens) == 1
    assert fake.tokens[0]["grant_type"] == ["client_credentials"]


def test_database_failure_compensates_with_the_scoped_client(provisioner, monkeypatch):
    from precis_mcp import admin_ops
    from tests.fakes.fake_platform_db import FakePlatformDB
    _, fake = provisioner
    db = FakePlatformDB()
    monkeypatch.setattr("precis_mcp.db.query_platform", db.query)
    def fail_insert(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr("precis_mcp.db.execute_platform", fail_insert)
    monkeypatch.setattr("precis_mcp.auth.ensure_user_directory", lambda uid: None)
    with pytest.raises(RuntimeError, match="database unavailable"):
        admin_ops.create_user(id="new-user", actor="operator", password="temporary-password")
    assert db.users == []
    assert any(r.method == "DELETE" and r.url.path.endswith("/users/user-uuid") for r in fake.requests)
    assert all(t["grant_type"] == ["client_credentials"] for t in fake.tokens)
