# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Configurable identity-claim resolution — gap 2 (precis_mcp.auth.resolve_user_id).

Component class because importing precis_mcp.auth pulls the db client; the DB
lookup for the external_id column is patched.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from precis_mcp import auth
from precis_mcp.auth import AuthError

_VARS = ("PRECIS_IDENTITY_CLAIM", "PRECIS_IDENTITY_COLUMN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _VARS:
        monkeypatch.delenv(k, raising=False)


# --- default behavior (no env) — unchanged from the shipped path ------------


def test_default_uses_precis_user_id():
    assert auth.resolve_user_id(
        {"precis_user_id": "jane", "preferred_username": "x"}
    ) == "jane"


def test_default_falls_back_to_preferred_username():
    assert auth.resolve_user_id({"preferred_username": "jane"}) == "jane"


def test_default_missing_claim_returns_none():
    assert auth.resolve_user_id({"sub": "kc-uuid"}) is None


def test_column_id_does_no_db_lookup():
    with patch("precis_mcp.auth.query_platform") as q:
        assert auth.resolve_user_id({"precis_user_id": "jane"}) == "jane"
        q.assert_not_called()


# --- explicit claim selection ----------------------------------------------


def test_explicit_claim_selected(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_CLAIM", "oid")
    assert auth.resolve_user_id({"oid": "GUID", "precis_user_id": "ignored"}) == "GUID"


def test_explicit_claim_has_no_fallback(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_CLAIM", "oid")
    # preferred_username is the *default* fallback only — not when a claim is named.
    assert auth.resolve_user_id({"preferred_username": "jane"}) is None


# --- external_id column join (mode C) --------------------------------------


def test_external_id_column_lookup(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_CLAIM", "oid")
    monkeypatch.setenv("PRECIS_IDENTITY_COLUMN", "external_id")
    captured = {}

    def fake_q(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"id": "jane"}]

    with patch("precis_mcp.auth.query_platform", side_effect=fake_q):
        assert auth.resolve_user_id({"oid": "GUID-123"}) == "jane"
    assert "external_id" in captured["sql"]
    assert captured["params"] == ("GUID-123",)


def test_external_id_no_match_returns_none(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_COLUMN", "external_id")
    with patch("precis_mcp.auth.query_platform", return_value=[]):
        assert auth.resolve_user_id({"precis_user_id": "GUID"}) is None


def test_disallowed_column_raises(monkeypatch):
    monkeypatch.setenv("PRECIS_IDENTITY_COLUMN", "email")  # not allow-listed
    with pytest.raises(AuthError, match="PRECIS_IDENTITY_COLUMN"):
        auth.resolve_user_id({"precis_user_id": "x"})
