# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""The open multi-user entrypoint validates PRECIS_AUTH_MODE at startup.

Verifies the lifespan wiring in precis_mcp/app_open.py — the resolver/validator
itself is unit-tested in tests/unit/test_auth_mode.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from precis_mcp.auth_mode import AuthModeError


def test_starts_with_default_keycloak(monkeypatch):
    monkeypatch.delenv("PRECIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    from precis_mcp.app_open import app

    with TestClient(app) as c:  # enters the lifespan
        assert c.get("/health").json() == {"status": "ok"}


def test_oidc_without_issuer_fails_startup(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "oidc")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    from precis_mcp.app_open import app

    with pytest.raises(AuthModeError):
        with TestClient(app):
            pass


def test_devkey_rejected_on_multiuser_entrypoint(monkeypatch):
    monkeypatch.setenv("PRECIS_AUTH_MODE", "devkey")
    from precis_mcp.app_open import app

    with pytest.raises(AuthModeError, match="single-user"):
        with TestClient(app):
            pass
