# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared pytest hooks and fixtures for the public Précis MCP contracts."""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import pytest

from tests.fakes.fake_clickhouse import FakeClickHouseClient
from tests.fakes.fake_platform_db import FakePlatformDB


# Push/upload tests use a synthetic binding-token secret and short DB timeouts.
os.environ.setdefault("INGEST_BINDING_JWT_SECRET", "test-binding-jwt-secret-not-for-production-use")
os.environ.setdefault("PG_POOL_TIMEOUT", "1")
os.environ.setdefault("PG_CONNECT_TIMEOUT", "1")
logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_OPEN_TESTS_MANIFEST = _TESTS_ROOT / "open_tests.txt"


_FORBIDDEN_IN_UNIT: frozenset[str] = frozenset({
    "psycopg",
    "psycopg2",
    "clickhouse_connect",
    "redis",
    "anthropic",
    "openai",
    "boto3",
    "fakeredis",
    "langgraph.checkpoint.postgres",
})


def _imports_in_file(path: Path) -> set[str]:
    """Return the module names imported in a Python source file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def _forbidden_imports(imports: set[str]) -> set[str]:
    """Subset of `imports` that hits a forbidden top-level module."""
    bad: set[str] = set()
    for name in imports:
        for forbidden in _FORBIDDEN_IN_UNIT:
            if name == forbidden or name.startswith(f"{forbidden}."):
                bad.add(name)
                break
    return bad


def _load_open_tests() -> frozenset[Path]:
    """Resolve the open-test manifest to a set of absolute paths."""
    try:
        raw = _OPEN_TESTS_MANIFEST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    base = _TESTS_ROOT
    out: set[Path] = set()
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add((base / line).resolve())
    return frozenset(out)


def _commercial_imports(imports: set[str]) -> set[str]:
    """Subset of `imports` that reaches the `precis` package.

    `precis_mcp` is the open package and is excluded by construction — it does
    not start with ``precis.`` (the next char is ``_``).
    """
    return {n for n in imports if n == "precis" or n.startswith("precis.")}


def pytest_collection_modifyitems(config, items):
    """Auto-apply `unit`/`component`/`e2e` markers by directory; fail the run
    if a `tests/unit/` file imports a forbidden real-service client, or if a
    file listed in `tests/open_tests.txt` imports the `precis`
    package.

    Markers follow the test directory: `unit`, `component`, or `e2e`.
    """
    violations: list[tuple[str, list[str]]] = []
    open_violations: list[tuple[str, list[str]]] = []
    imports_cache: dict[Path, set[str]] = {}
    open_tests = _load_open_tests()

    def _imports(path: Path) -> set[str]:
        if path not in imports_cache:
            imports_cache[path] = _imports_in_file(path)
        return imports_cache[path]

    for item in items:
        path_str = str(item.fspath)
        path = Path(path_str)

        if "/tests/unit/" in path_str:
            item.add_marker(pytest.mark.unit)
            bad = _forbidden_imports(_imports(path))
            if bad:
                violations.append((path_str, sorted(bad)))
        elif "/tests/component/" in path_str:
            item.add_marker(pytest.mark.component)
        elif "/tests/e2e/" in path_str:
            item.add_marker(pytest.mark.e2e)

        if path.resolve() in open_tests:
            commercial = _commercial_imports(_imports(path))
            if commercial:
                open_violations.append((path_str, sorted(commercial)))

    error_blocks: list[str] = []
    if violations:
        lines = [
            "Unit tests with forbidden real-service imports:",
        ]
        for p, mods in _dedupe(violations):
            lines.append(f"  {p}: {', '.join(mods)}")
        lines.append(
            "Fix: replace the real-client import with a shared fake from "
            "tests/fakes/, or move the test to tests/component/."
        )
        error_blocks.append("\n".join(lines))

    if open_violations:
        lines = [
            "Open tests importing the `precis` package (see "
            "tests/open_tests.txt):",
        ]
        for p, mods in _dedupe(open_violations):
            lines.append(f"  {p}: {', '.join(mods)}")
        lines.append(
            "Fix: drop the `precis` import (use `precis_mcp` equivalents), or "
            "remove the file from tests/open_tests.txt if it genuinely "
            "belongs to the Précis platform."
        )
        error_blocks.append("\n".join(lines))

    if error_blocks:
        raise pytest.UsageError("\n\n" + "\n\n".join(error_blocks))


def _dedupe(
    violations: list[tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """First occurrence per path, preserving order."""
    seen: set[str] = set()
    unique: list[tuple[str, list[str]]] = []
    for p, mods in violations:
        if p in seen:
            continue
        seen.add(p)
        unique.append((p, mods))
    return unique


@pytest.fixture
def db() -> FakePlatformDB:
    """A fresh fake of the public platform database tables."""
    return FakePlatformDB()


@pytest.fixture(autouse=True)
def _reset_scenario_registry_cache():
    """The process-wide TTL registry cache must not leak across tests —
    each test wires its own fake ClickHouse client."""
    from precis_mcp.engine.scenario_registry import (
        invalidate_scenario_registry_cache,
    )
    invalidate_scenario_registry_cache()
    yield
    invalidate_scenario_registry_cache()


@pytest.fixture(autouse=True)
def _isolate_analytical_resources():
    """Process-owned SQL sessions/executors must not escape a test's fake drivers."""
    from precis_mcp.analytical import close_analytical_resources
    close_analytical_resources()
    yield
    close_analytical_resources()


@pytest.fixture
def ch_client() -> FakeClickHouseClient:
    """In-memory ClickHouse-client substitute. See `tests/fakes/fake_clickhouse.py`.

    Replaces the inline `patch("…get_clickhouse_client")` + `MagicMock`
    pattern duplicated across ~14 test files (50+ call sites). Tests that
    need canned responses configure via `ch_client.set_response(...)` or
    `ch_client.push(...)`.
    """
    return FakeClickHouseClient()


@pytest.fixture(scope="module")
def catalogue():
    """Load the bundled instance catalogue once per test module."""
    from precis_mcp.engine.catalogue import load_catalogue

    return load_catalogue(str(_TESTS_ROOT.parent / "instance" / "catalogue"))


@pytest.fixture(scope="module")
def scenario_registry():
    """Semantic scenario registry fixture matching the active demo catalogue."""
    from precis_mcp.engine.scenario_registry import ScenarioRegistry

    return ScenarioRegistry.from_rows([
        {
            "scenario_id": "ACTUALS",
            "alias": "actuals",
            "name": "Actuals",
            "description": "Historical actuals from the general ledger.",
            "kind": "ACTUAL",
            "status": "LOCKED",
        },
        {
            "scenario_id": "BUD-2026",
            "alias": "budget",
            "name": "Budget 2026",
            "description": "Approved annual budget for 2026.",
            "kind": "BUDGET",
            "status": "APPROVED",
        },
        {
            "scenario_id": "FC-2026-Q1",
            "alias": "forecast",
            "name": "Forecast Q1",
            "description": "Rolling forecast updated after Q1 2026 close.",
            "kind": "FORECAST",
            "status": "DRAFT",
            "base_scenario": "BUD-2026",
        },
        {
            "scenario_id": "FC-2026-Q2",
            "alias": "forecast_q2",
            "name": "Forecast Q2",
            "description": "Runtime forecast scenario for registry tests.",
            "kind": "FORECAST",
            "status": "DRAFT",
            "base_scenario": "BUD-2026",
        },
    ])


@pytest.fixture(autouse=True, scope="session")
def _patch_keycloak_verifier():
    """Decode synthetic bearer tokens without a live identity provider."""
    from precis_mcp import oidc

    def fake_verify(token: str) -> dict:
        if not isinstance(token, str) or not token.startswith("test:"):
            raise ValueError(f"verify_keycloak_token: not a test token: {token!r}")
        parts = token.split(":")
        if len(parts) < 2:
            raise ValueError(f"verify_keycloak_token: malformed test token: {token!r}")
        user_id = parts[1]
        claims = {
            "precis_user_id": user_id,
            "preferred_username": user_id,
            "sub": user_id,
            "iat": 0,
            "exp": 9_999_999_999,
        }
        audience = oidc.mcp_audience()
        if audience:
            claims["aud"] = [audience]
        return claims

    patcher = pytest.MonkeyPatch()
    patcher.setattr(oidc, "verify_keycloak_token", fake_verify)
    yield
    patcher.undo()


@pytest.fixture
def open_only(monkeypatch):
    """Isolate the base catalogue and rebuild transport caches without extensions."""
    from precis_mcp import dispatch
    from precis_mcp.mcp_external import server as transport

    monkeypatch.setattr(dispatch, "_EXTRA_TOOL_LOADERS", [])
    monkeypatch.setattr(dispatch, "_EXTRA_CATALOGUE", {})
    monkeypatch.setattr(transport, "_descriptors", None)
    monkeypatch.setattr(transport, "_schemas_by_name", None)
    monkeypatch.setattr(transport, "_wrappers", None)
    yield
