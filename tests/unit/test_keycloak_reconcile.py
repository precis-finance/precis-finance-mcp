# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Keycloak scope repairs preserve the representation required by its API."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def reconcile(monkeypatch):
    monkeypatch.setenv("KC_BOOTSTRAP_ADMIN_PASSWORD", "test-only-password")
    path = Path(__file__).parents[2] / "deploy/keycloak/reconcile.py"
    spec = importlib.util.spec_from_file_location("keycloak_reconcile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("visibility", ["false", None, "true"])
def test_scope_repair_preserves_keycloak_representation(reconcile, monkeypatch, visibility):
    original = {
        "id": "scope-id", "name": "precis-mcp", "protocol": "openid-connect",
        "attributes": {"display.on.consent.screen": "false"},
        "protocolMappers": [{"id": "mapper-id", "name": "mcp-audience"}],
    }
    if visibility is not None:
        original["attributes"]["include.in.token.scope"] = visibility
    writes = []

    def admin(method, path, data=None):
        assert path == "/client-scopes/scope-id"
        if method == "GET":
            return deepcopy(original)
        assert method == "PUT"
        writes.append(deepcopy(data))

    monkeypatch.setattr(reconcile, "_admin", admin)
    reconcile._ensure_scope_visible("scope-id")
    if visibility == "true":
        assert writes == []
    else:
        expected = deepcopy(original)
        expected["attributes"]["include.in.token.scope"] = "true"
        assert writes == [expected]
