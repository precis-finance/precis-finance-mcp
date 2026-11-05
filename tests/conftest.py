# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pytest setup for the published Précis MCP test suite.

Decorated fixtures and hooks are imported for pytest discovery.
"""
from tests.support.open_pytest import (  # noqa: F401
    _isolate_analytical_resources,
    _patch_keycloak_verifier,
    _reset_scenario_registry_cache,
    catalogue,
    ch_client,
    db,
    open_only,
    pytest_collection_modifyitems,
    scenario_registry,
)
