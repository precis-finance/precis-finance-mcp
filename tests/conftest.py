# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared pytest configuration, hooks, and fixtures.

This file is the root of the test-runtime contract that complements the
schema rules in `tests/CLAUDE.md`. It carries:

- Test-environment env var setup (must run before any precis import).
- The directory-→-marker auto-application hook (`unit`, `component`, `e2e`
  derive from the test's location under `tests/`).
- The I/O import guard that fails the run when a `tests/unit/` test
  imports a real-service client.
- The open-core test boundary guard that fails the run when a file listed
  in `tests/open_tests.txt` imports the commercial `precis` package
  (precis_mcp_package_spec.md §6 / R3).
- The canonical shared fixtures (`db`, `ch_client`, `auth_token`,
  `auth_headers`, `redis`, `client`).

`tests/CLAUDE.md` is the source of truth for what each fixture promises and
which class of test should use it. Marker registration lives in
`pyproject.toml` `[tool.pytest.ini_options]`.
"""

from __future__ import annotations

import ast
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Environment setup — must run before any precis import.
# ---------------------------------------------------------------------------

# Set CHECKPOINT_DIR before any module import so agui.py's module-level init
# doesn't fail trying to create /data/checkpoints locally.
if "CHECKPOINT_DIR" not in os.environ:
    os.environ["CHECKPOINT_DIR"] = os.path.join(tempfile.gettempdir(), "test_checkpoints")

# Remove JWT_DEV_USER if present (e.g. loaded from .env by precis.server).
# Auth tests expect 401 on unauthenticated requests; JWT_DEV_USER bypasses that.
os.environ.pop("JWT_DEV_USER", None)

# Ingestion binding-token secret — verify_binding_token raises without it.
# Tests that mint binding tokens for the push / upload endpoints use this.
os.environ.setdefault("INGEST_BINDING_JWT_SECRET", "test-binding-jwt-secret-not-for-production-use")

# Platform-DB pool: fail fast in tests rather than the production-default 10s
# acquire timeout, so a test that reaches the real pool (no test DB, or wrong
# creds) errors in ~1s instead of stalling the suite. Tests that need platform
# data fake `query_platform` / `execute_platform` per call site. Silence
# psycopg_pool's connect-retry log spam while we're at it.
os.environ.setdefault("PG_POOL_TIMEOUT", "1")
os.environ.setdefault("PG_CONNECT_TIMEOUT", "1")
import logging as _logging  # noqa: E402

_logging.getLogger("psycopg.pool").setLevel(_logging.CRITICAL)


# ---------------------------------------------------------------------------
# Directory-→-marker hook + I/O import guard for tests/unit/.
# ---------------------------------------------------------------------------

# Forbidden imports inside `tests/unit/` — real-service client libraries and
# their async cousins. Matching is exact module name or any sub-module
# beneath. Replace with shared fakes in `tests/fakes/`, or move the test to
# `tests/component/`. See `tests/CLAUDE.md` "Class 1 — unit tests".
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


# ---------------------------------------------------------------------------
# Open-core test boundary guard — see tests/open_tests.txt and
# precis_mcp_package_spec.md §6 / R3.
#
# The manifest lists every test that exercises ONLY the open `precis_mcp`
# package and is therefore extracted to the open repo at the R4 cut. Those
# files must not import the commercial `precis` package — otherwise extraction
# breaks. This is the "extraction check" the spec calls for: the package-level
# import-linter contract checks `precis_mcp` vs `precis` but not `tests/`, so a
# new open test could import `precis` unnoticed without this guard.
# ---------------------------------------------------------------------------

_OPEN_TESTS_MANIFEST = Path(__file__).parent / "open_tests.txt"


def _load_open_tests() -> frozenset[Path]:
    """Resolve the open-test manifest to a set of absolute paths."""
    try:
        raw = _OPEN_TESTS_MANIFEST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    base = Path(__file__).parent
    out: set[Path] = set()
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add((base / line).resolve())
    return frozenset(out)


def _commercial_imports(imports: set[str]) -> set[str]:
    """Subset of `imports` that reaches the commercial `precis` package.

    `precis_mcp` is the open package and is excluded by construction — it does
    not start with ``precis.`` (the next char is ``_``).
    """
    return {n for n in imports if n == "precis" or n.startswith("precis.")}


def pytest_collection_modifyitems(config, items):
    """Auto-apply `unit`/`component`/`e2e` markers by directory; fail the run
    if a `tests/unit/` file imports a forbidden real-service client, or if a
    file listed in `tests/open_tests.txt` imports the commercial `precis`
    package.

    The marker assignment is mechanical: a test under `tests/unit/` gets the
    `unit` marker; same for `component` and `e2e`. Tests still at the top of
    `tests/` (pre-Step-2-migration) get no auto-marker; existing
    `integration` and `api` markers carried by the test continue to work.
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
            "Unit tests with forbidden real-service imports (see tests/CLAUDE.md "
            '"Class 1 — unit tests"):',
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
            "Open-core tests importing the commercial `precis` package (see "
            "tests/open_tests.txt and precis_mcp_package_spec.md §6):",
        ]
        for p, mods in _dedupe(open_violations):
            lines.append(f"  {p}: {', '.join(mods)}")
        lines.append(
            "Fix: drop the `precis` import (use `precis_mcp` equivalents), or "
            "remove the file from tests/open_tests.txt if it is genuinely "
            "commercial."
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


# ---------------------------------------------------------------------------
# Shared fixtures — see tests/CLAUDE.md "Shared fakes" and "Test factories".
#
# Existing test files that define their own `db` / `client` / etc. fixtures
# continue to win (closer scope). These conftest fixtures activate during
# Step 2 of the migration as each file's inline fixture is removed.
# ---------------------------------------------------------------------------

from tests.fakes.fake_platform_db import FakePlatformDB  # noqa: E402
from tests.fakes.fake_clickhouse import FakeClickHouseClient  # noqa: E402
from tests.fakes.fake_redis import FakeRedis  # noqa: E402
from tests.factories.auth import make_auth_headers, make_token  # noqa: E402

# Register the commercial tool set into the open dispatch core so
# build_descriptors / build_agent_tools / _load_all_tools see the full tool
# surface in tests, mirroring agui startup. Idempotent (ADR-0008). Without
# this, the dispatch core loads only the open read-only tools.
#
# Guarded so this same conftest works unchanged in the open `precis-mcp` repo
# (precis_mcp_package_spec.md §6 / R3): there the commercial `precis` package is
# absent, so there is nothing to register and the open read-only set is correct.
# The other commercial touch — `from precis.agui import app` — is lazy inside the
# `client` fixture, which no open test uses, so it never fires in the open repo.
try:
    from precis.agent.commercial_tools import install as _install_commercial_tools  # noqa: E402

    _install_commercial_tools()
except ImportError:
    pass


@pytest.fixture
def db() -> FakePlatformDB:
    """In-memory platform-DB substitute. See `tests/fakes/fake_platform_db.py`.

    Replaces the inline `def db(): return FakePlatformDB()` fixture currently
    duplicated across ~18 test files.
    """
    return FakePlatformDB()


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
    """Loaded `Catalogue` from `precis/catalogue/`.

    Replaces the inline `def catalogue(): return load_catalogue(CATALOGUE_DIR)`
    fixture duplicated across 15 test files. Files that need a different
    catalogue shape (e.g. programmatic minimal catalogue for plan-validation
    edge cases) keep their own local fixture.

    Module-scoped because (a) the catalogue is immutable in production and
    treated as read-only, and (b) several call-site fixtures pre-migration
    were already module-scoped (`pnl_display_items` in test_formatter, etc.),
    which would scope-mismatch against a function-scoped catalogue.
    """
    from pathlib import Path

    from precis_mcp.engine.catalogue import load_catalogue

    catalogue_dir = (
        Path(__file__).resolve().parent.parent
        / "instance"
        / "catalogue"
    )
    return load_catalogue(str(catalogue_dir))


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
    """Stub precis_mcp.oidc.verify_keycloak_token for the test session.

    Production validates Keycloak-issued RS256 JWTs via JWKS — tests can't
    plausibly produce real ones.  Instead, tests/factories/auth.py emits
    sentinel strings of the form ``test:<user_id>``; this fixture installs
    a verifier that turns them back into the claims shape the middleware
    expects.

    Session-scoped so the patch is in place before TestClient(app) boots.
    """
    from _pytest.monkeypatch import MonkeyPatch

    def fake_verify(token: str) -> dict:
        if not isinstance(token, str) or not token.startswith("test:"):
            raise ValueError(f"verify_keycloak_token: not a test token: {token!r}")
        parts = token.split(":")
        if len(parts) < 2:
            raise ValueError(f"verify_keycloak_token: malformed test token: {token!r}")
        user_id = parts[1]
        return {
            "precis_user_id": user_id,
            "preferred_username": user_id,
            "sub": user_id,
            "iat": 0,
            "exp": 9_999_999_999,
        }

    mp = MonkeyPatch()
    mp.setattr("precis_mcp.oidc.verify_keycloak_token", fake_verify)
    yield
    mp.undo()


@pytest.fixture
def redis():
    """In-memory Redis substitute via `fakeredis`. See `tests/fakes/fake_redis.py`."""
    return FakeRedis()


@pytest.fixture
def auth_token() -> str:
    """JWT for the default test user (`testuser`).

    Tests that need a different user import `make_token` from
    `tests/factories/auth.py` directly.
    """
    return make_token("testuser")


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer-header dict for FastAPI TestClient calls as the default test user.

    `{"Authorization": "Bearer <jwt>"}`. Tests that need a different user
    import `make_auth_headers` from `tests/factories/auth.py` directly.
    """
    return make_auth_headers("testuser")


@pytest.fixture(scope="module")
def client() -> Iterator:
    """FastAPI `TestClient` for the agui app, with ClickHouse and Redis
    patched to in-memory fakes. Module-scoped because agui app boot is
    non-trivial.

    Replaces the inline `patch("get_clickhouse_client") +
    patch("get_redis_client") + MagicMock + TestClient(app)` pattern
    duplicated across 7 endpoint tests.
    """
    fake_ch = FakeClickHouseClient()
    fake_rd = FakeRedis()
    with patch("precis_mcp.db.get_clickhouse_client", return_value=fake_ch), \
         patch("precis_mcp.db.get_redis_client", return_value=fake_rd):
        from fastapi.testclient import TestClient

        from precis.agui import app
        with TestClient(app) as tc:
            yield tc
