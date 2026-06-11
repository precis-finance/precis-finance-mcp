# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""ClickHouse schema provisioner for precis-mcp — the CH analog of migrate.py.

Applies the configured instance to ClickHouse and seeds the package-owned
``semantic.scenarios``. **Schema-only**: it creates databases, tables, views,
and the scenario-registry rows; it does *not* load fact/actuals data (ingestion
does that). Idempotent throughout — the runners use ``CREATE … IF NOT EXISTS`` /
``CREATE OR REPLACE VIEW`` and scenario seeding is insert-if-absent — so a
re-run against an already-provisioned (e.g. operator-pre-populated BYO)
ClickHouse reconciles rather than clobbering.

There is no "platform schema" independent of an instance: ``live.*`` and the
``semantic.*`` views are operator-authored SQL under ``instance/``. The
provisioner is "apply the configured instance + seed the package-owned
registry."

Step order (spec §4.1 — `docs/clickhouse_provisioning_spec.md`)::

    1. live            live.* + staging.* tables   (instance/live/*.sql)
    2. scenarios       semantic.scenarios + seed   (instance/scenarios.yml)
    3. <extension>     e.g. the commercial planning runner — registered via
                       register_step(); runs AFTER scenarios and BEFORE the
                       semantic views
    4. semantic_views  semantic.* views            (instance/semantic/{dims,views})

The semantic views run **last** in every scope: an instance view may reference a
table a commercial extension step creates (e.g. ``planning.entries``), and
ClickHouse validates view references at CREATE time. ``--scope open`` skips the
extension steps (an actuals-only open deployment has none).

Run::

    python -m precis_mcp.clickhouse_init                 # all registered steps
    python -m precis_mcp.clickhouse_init --scope open    # open tier only
    python -m precis_mcp.clickhouse_init --dry-run        # show plan, touch nothing
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from precis_mcp.ingestion import (
    live_ddl_runner,
    scenario_runner,
    semantic_runner,
)
from precis_mcp.observability import get_logger

_logger = get_logger("clickhouse_init")

# A provisioning step: (name, fn) where fn(instance_dir, ch_client) applies it.
Step = tuple[str, Callable[[Path, Any], Any]]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Extension steps injected by the commercial package (the planning runner).
# They run after `scenarios` and before `semantic_views` — see module docstring.
_EXTENSION_STEPS: list[Step] = []


def register_step(name: str, fn: Callable[[Path, Any], Any]) -> None:
    """Register a pre-semantic-views provisioning step (e.g. the commercial
    planning runner). Idempotent by name — re-registering replaces the prior
    entry rather than duplicating it. Order of first registration is preserved.
    """
    global _EXTENSION_STEPS
    existing = next((i for i, (n, _) in enumerate(_EXTENSION_STEPS) if n == name), None)
    if existing is not None:
        _EXTENSION_STEPS[existing] = (name, fn)
    else:
        _EXTENSION_STEPS.append((name, fn))


def default_instance_dir() -> Path:
    """The instance config root (`<repo>/instance`), the same one the runners
    and the synthetic seeder consume. Mounted at `/app/instance` in the bundle."""
    return PROJECT_ROOT / "instance"


def _open_steps() -> tuple[Step, Step]:
    return (
        ("live", lambda d, ch: live_ddl_runner.apply_all(d / "live", ch)),
        ("scenarios", lambda d, ch: scenario_runner.apply(d / "scenarios.yml", ch)),
    )


def plan(scope: str, extension_steps: list[Step] | None = None) -> list[Step]:
    """The ordered step list for a scope. Pure — builds the plan, runs nothing.

    `extension_steps` defaults to the module registry; tests inject their own to
    avoid touching global state.
    """
    ext = _EXTENSION_STEPS if extension_steps is None else extension_steps
    steps: list[Step] = list(_open_steps())
    if scope != "open":
        steps.extend(ext)
    steps.append(
        ("semantic_views", lambda d, ch: semantic_runner.apply_all(d / "semantic", ch))
    )
    return steps


def provision(
    instance_dir: Path,
    ch_client: Any,
    *,
    scope: str = "all",
    extension_steps: list[Step] | None = None,
) -> list[str]:
    """Run the provisioning plan against `ch_client`. Returns the ordered names
    of the steps run. Step failures propagate (no partial-success swallowing)."""
    ran: list[str] = []
    for name, fn in plan(scope, extension_steps):
        _logger.info("clickhouse_init.step_start", step=name)
        fn(instance_dir, ch_client)
        ran.append(name)
        _logger.info("clickhouse_init.step_done", step=name)
    return ran


# ---------------------------------------------------------------------------
# Preflight / conformance check (--check) — the nginx -t for provisioning.
# Validates WITHOUT applying: catalogue parses, the semantic views it names
# exist in ClickHouse, semantic.scenarios is seeded, and (commercial extension)
# the planning tables carry the expected columns. Spec §6.2(a).
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


Check = tuple[str, Callable[[Path, Any], list["CheckResult"]]]

# Commercial conformance checks (e.g. planning-schema drift), registered the
# same way as provisioning steps. Run only for non-open scope.
_EXTENSION_CHECKS: list[Check] = []


def register_check(name: str, fn: Callable[[Path, Any], list[CheckResult]]) -> None:
    """Register a commercial conformance check into the preflight. Idempotent by
    name (re-registering replaces); first-registration order preserved."""
    global _EXTENSION_CHECKS
    existing = next(
        (i for i, (n, _) in enumerate(_EXTENSION_CHECKS) if n == name), None
    )
    if existing is not None:
        _EXTENSION_CHECKS[existing] = (name, fn)
    else:
        _EXTENSION_CHECKS.append((name, fn))


def _existing_semantic_objects(ch_client: Any) -> set[str]:
    res = ch_client.query(
        "SELECT concat(database, '.', name) FROM system.tables "
        "WHERE database = 'semantic'"
    )
    return {row[0] for row in res.result_rows}


def _check_semantic_views(catalogue: Any, ch_client: Any) -> list[CheckResult]:
    """Every clickhouse-backed domain's `source_view` must exist in ClickHouse."""
    existing = _existing_semantic_objects(ch_client)
    results: list[CheckResult] = []
    for key, domain in catalogue.domains.items():
        if getattr(domain, "backend_kind", "clickhouse") != "clickhouse":
            continue
        sv = domain.source_view
        ok = sv in existing
        results.append(
            CheckResult(
                f"view:{sv}",
                ok,
                "" if ok else f"domain {key!r}: {sv} not found in ClickHouse",
            )
        )
    return results


def _check_scenarios(ch_client: Any) -> CheckResult:
    try:
        res = ch_client.query("SELECT count() FROM semantic.scenarios")
        n = res.result_rows[0][0] if res.result_rows else 0
    except Exception as exc:  # noqa: BLE001 — report unreadable as a failed check
        return CheckResult("scenarios", False, f"semantic.scenarios unreadable: {exc}")
    return CheckResult(
        "scenarios", n > 0, f"{n} row(s)" if n else "semantic.scenarios is empty"
    )


def check(
    instance_dir: Path,
    ch_client: Any,
    *,
    scope: str = "all",
    extension_checks: list[Check] | None = None,
    _load: Callable[[str], Any] | None = None,
) -> list[CheckResult]:
    """Run the preflight. Returns one CheckResult per check; applies nothing.

    `_load` overrides the catalogue loader (tests inject a stub/raiser); it
    defaults to the engine's `load_and_validate`.
    """
    from precis_mcp.engine import CatalogueError, load_and_validate

    loader = _load or load_and_validate
    results: list[CheckResult] = []

    catalogue = None
    try:
        catalogue = loader(str(instance_dir / "catalogue"))
        results.append(CheckResult("catalogue", True, "parses and validates"))
    except CatalogueError as exc:
        results.append(CheckResult("catalogue", False, str(exc)))

    if catalogue is not None:
        results.extend(_check_semantic_views(catalogue, ch_client))
    results.append(_check_scenarios(ch_client))

    ext = _EXTENSION_CHECKS if extension_checks is None else extension_checks
    if scope != "open":
        for _name, fn in ext:
            results.extend(fn(instance_dir, ch_client))

    return results


def _describe(instance_dir: Path, scope: str) -> None:
    """Print the dry-run plan: the steps and the instance artefacts each touches."""
    for name, _ in plan(scope):
        if name == "live":
            n = len(sorted((instance_dir / "live").glob("*.sql")))
            print(f"  live            {n} table file(s) from instance/live/*.sql")
        elif name == "scenarios":
            f = instance_dir / "scenarios.yml"
            here = "present" if f.exists() else "MISSING"
            print(f"  scenarios       instance/scenarios.yml ({here})")
        elif name == "semantic_views":
            sem = instance_dir / "semantic"
            n = len(sorted(sem.glob("**/*.sql")))
            print(f"  semantic_views  {n} view file(s) from instance/semantic/")
        else:
            print(f"  {name}  (extension step)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision the ClickHouse schema for precis-mcp "
        "(schema-only; ingestion loads data separately)."
    )
    parser.add_argument(
        "--scope",
        choices=("all", "open"),
        default="all",
        help="'open' runs the open tier only (live + scenarios + semantic views); "
        "'all' (default) also runs registered extension steps (e.g. commercial "
        "planning). For an open-only install the two are equivalent.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and the instance artefacts it would touch; apply nothing.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Preflight: validate the catalogue and confirm ClickHouse conformance "
        "(semantic views exist, scenarios seeded, planning schema). Applies "
        "nothing; exits non-zero on any failure.",
    )
    parser.add_argument(
        "--instance-dir",
        default=None,
        help="Override the instance config root (default: <repo>/instance).",
    )
    args = parser.parse_args(argv)

    # Resolve *_FILE secrets (CHPASSWORD_FILE → CHPASSWORD) and load .env so the
    # command works inside the api container and on a native host alike. Import
    # is the activation — see precis_mcp/secrets.py. Mirrors migrate.py.
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass
    import precis_mcp.secrets  # noqa: F401

    instance_dir = (
        Path(args.instance_dir) if args.instance_dir else default_instance_dir()
    )

    if args.dry_run:
        print(f"clickhouse_init plan (scope={args.scope}, instance={instance_dir}):")
        _describe(instance_dir, args.scope)
        return 0

    from precis_mcp.db import get_clickhouse_client

    if args.check:
        ch = get_clickhouse_client()
        print(f"Preflight (scope={args.scope}) from {instance_dir}:")
        results = check(instance_dir, ch, scope=args.scope)
        for r in results:
            mark = "ok  " if r.ok else "FAIL"
            suffix = f" — {r.detail}" if r.detail else ""
            print(f"  [{mark}] {r.name}{suffix}")
        failed = [r for r in results if not r.ok]
        if failed:
            print(f"{len(failed)} check(s) failed.", file=sys.stderr)
            return 1
        print("All checks passed.")
        return 0

    ch = get_clickhouse_client()
    print(f"Provisioning ClickHouse (scope={args.scope}) from {instance_dir} ...")
    try:
        ran = provision(instance_dir, ch, scope=args.scope)
    except Exception as exc:  # noqa: BLE001 — surface the failure, exit non-zero
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Done. Ran: {', '.join(ran)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
