# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the PRECIS_AUTH_MODE selector (precis_mcp/auth_mode.py)."""
from __future__ import annotations

import pytest

from precis_mcp import auth_mode as am

_VARS = ("PRECIS_AUTH_MODE", "OIDC_ISSUER", "MCP_DEV_KEY", "PRECIS_IDENTITY_COLUMN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _VARS:
        monkeypatch.delenv(k, raising=False)


# --- resolve_auth_mode -----------------------------------------------------


def test_unset_with_default_returns_default(monkeypatch):
    assert am.resolve_auth_mode(default=am.AuthMode.KEYCLOAK) is am.AuthMode.KEYCLOAK


def test_unset_without_default_raises(monkeypatch):
    with pytest.raises(am.AuthModeError):
        am.resolve_auth_mode()


@pytest.mark.parametrize("value,expected", [
    ("devkey", am.AuthMode.DEVKEY),
    ("keycloak", am.AuthMode.KEYCLOAK),
    ("oidc", am.AuthMode.OIDC),
    ("OIDC", am.AuthMode.OIDC),       # case-insensitive
    ("  keycloak  ", am.AuthMode.KEYCLOAK),  # trimmed
])
def test_valid_values(monkeypatch, value, expected):
    monkeypatch.setenv("PRECIS_AUTH_MODE", value)
    assert am.resolve_auth_mode() is expected


def test_header_mode_d_rejected(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "header")
    with pytest.raises(am.AuthModeError, match="mode D"):
        am.resolve_auth_mode()


def test_unknown_value_raises(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "ldap")
    with pytest.raises(am.AuthModeError):
        am.resolve_auth_mode()


# --- validate_auth_config --------------------------------------------------


def test_oidc_requires_issuer(monkeypatch):
    with pytest.raises(am.AuthModeError, match="OIDC_ISSUER"):
        am.validate_auth_config(am.AuthMode.OIDC)


def test_oidc_ok_with_issuer(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://tenant.auth0.com/")
    am.validate_auth_config(am.AuthMode.OIDC)  # no raise


def test_keycloak_needs_nothing(monkeypatch):
    am.validate_auth_config(am.AuthMode.KEYCLOAK)  # dev defaults suffice


def test_devkey_requires_dev_key(monkeypatch):
    with pytest.raises(am.AuthModeError, match="MCP_DEV_KEY"):
        am.validate_auth_config(am.AuthMode.DEVKEY)


def test_validate_rejects_bad_identity_column(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_COLUMN", "email")  # not allow-listed
    with pytest.raises(am.AuthModeError, match="PRECIS_IDENTITY_COLUMN"):
        am.validate_auth_config(am.AuthMode.KEYCLOAK)


def test_validate_allows_external_id_column(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_COLUMN", "external_id")
    am.validate_auth_config(am.AuthMode.KEYCLOAK)  # no raise


# --- resolve_for_multiuser (the app_open entrypoint contract) --------------


def test_multiuser_defaults_to_keycloak(monkeypatch):
    assert am.resolve_for_multiuser() is am.AuthMode.KEYCLOAK


def test_multiuser_rejects_devkey(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "devkey")
    with pytest.raises(am.AuthModeError, match="single-user"):
        am.resolve_for_multiuser()


def test_multiuser_oidc_requires_issuer(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "oidc")
    with pytest.raises(am.AuthModeError, match="OIDC_ISSUER"):
        am.resolve_for_multiuser()


def test_multiuser_oidc_ok_with_issuer(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", "https://login.example/realms/x")
    assert am.resolve_for_multiuser() is am.AuthMode.OIDC


# --- resolve_for_devkey (the dev-server entrypoint contract) ---------------


def test_devkey_resolver_defaults_to_devkey(monkeypatch):
    assert am.resolve_for_devkey() is am.AuthMode.DEVKEY


def test_devkey_resolver_accepts_explicit_devkey(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "devkey")
    assert am.resolve_for_devkey() is am.AuthMode.DEVKEY


def test_devkey_resolver_rejects_multiuser_modes(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "keycloak")
    with pytest.raises(am.AuthModeError, match="multi-user"):
        am.resolve_for_devkey()
