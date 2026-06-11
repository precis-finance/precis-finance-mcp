# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""End-to-end test for the Ibis-driven ingestion pipeline.

One happy path: orchestrator runs a real `customer_pg__gl` push for a
single period against real Postgres + ClickHouse, then a
real `semantic.v_gl` query returns the freshly-landed rows.

Class 3 per `tests/CLAUDE.md` — marked `slow`. Skipped by default
(`pytest --slow` to opt in, or `make test-ci` to include in the full
suite). Auto-skips when the required env vars aren't set, so a CI
without a wired-up local stack just no-ops this file.

What this catches that the component/orchestrator tests can't:
  - Real CH REPLACE PARTITION semantics on a real MergeTree table
    (the orchestrator test uses a captured-SQL fake; CH could reject
    valid-looking SQL for engine-spec reasons the fake doesn't
    model).
  - Real Ibis-against-Postgres column-type coercion through to
    `clickhouse-connect.insert_df`.
  - Real Postgres `load_history` writes via the production
    `PostgresLoadHistoryWriter`.

The advisory-lock concurrency behaviour has its own focused e2e file
(`test_pg_advisory_lock_integration.py`, per ADR-0006).
"""

from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# Opt-in gate — skip cleanly if the deployment env isn't wired
# ---------------------------------------------------------------------------


REQUIRED_ENV = (
    "CHHOST",
    "CHPORT",
    "CUSTOMER_PG_HOST",
    "CUSTOMER_PG_DATABASE",
    "CUSTOMER_PG_USER",
)


def _env_ready() -> bool:
    return all(os.environ.get(k) for k in REQUIRED_ENV)


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _env_ready(),
        reason=(
            "ingestion e2e needs CH + PG env vars; set them or run "
            "`pytest --slow` from a deployment with the local stack up"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Happy-path full pipeline
# ---------------------------------------------------------------------------


def test_ingestion_e2e_one_period_lands_in_live_and_propagates_to_semantic():
    """Full extract → validate → swap → semantic view chain.

    Pre-conditions:
      - `instance/integrations/bindings/customer_pg__gl.yml` is on the
        active path (the script reads `_default_integrations_root()`).
      - PG's `gl.journal_postings` has at least one row for the
        chosen period.

    The test runs one binding for one period and asserts:
      1. The orchestrator returned `status=success`.
      2. `live.fact_gl` has the same row count for that period as the
         orchestrator reported in `rows_landed`.
      3. `semantic.v_gl` returns the same count for that period after
         the swap promoted staging into live.
    """
    from precis_mcp.db import get_clickhouse_client
    from precis_mcp.ingestion.orchestrator import run_binding
    from precis_mcp.ingestion.registry import IntegrationRegistry
    from precis_mcp.ingestion.wiring import (
        _default_integrations_root,
        build_default_context,
    )

    period = "2026-04"
    binding_id = "customer_pg__gl"

    registry_root = _default_integrations_root()
    registry = IntegrationRegistry.load(registry_root)
    if binding_id not in registry.bindings:
        pytest.skip(f"binding {binding_id!r} not in registry — env not seeded")

    ctx = build_default_context(registry)
    result = run_binding(ctx, binding_id, period, triggered_by="e2e:test")

    assert result.status == "success", f"orchestrator failed: {result.error}"
    assert result.rows_landed is not None and result.rows_landed > 0

    ch = get_clickhouse_client()
    live_count = ch.query(
        "SELECT count() FROM live.fact_gl WHERE period = %(p)s",
        parameters={"p": period},
    ).result_rows[0][0]
    semantic_count = ch.query(
        "SELECT count() FROM semantic.v_gl "
        "WHERE scenario='ACTUALS' AND period = %(p)s",
        parameters={"p": period},
    ).result_rows[0][0]

    assert live_count == result.rows_landed, (
        f"live.fact_gl[period={period}] = {live_count}, "
        f"expected {result.rows_landed}"
    )
    assert semantic_count == result.rows_landed, (
        f"semantic.v_gl[scenario=ACTUALS, period={period}] = {semantic_count}, "
        f"expected {result.rows_landed}"
    )


def test_ingestion_e2e_double_entry_balance_holds_after_load():
    """GL is a balanced double-entry ledger — the sum across all
    fs_lines for any one period should net to zero. A real load that
    lands an unbalanced slice would break planning / reporting
    consumers downstream; this test catches the regression early.
    """
    from precis_mcp.db import get_clickhouse_client

    period = "2026-04"
    ch = get_clickhouse_client()
    rows = ch.query(
        "SELECT round(sum(amount), 2) FROM semantic.v_gl "
        "WHERE scenario='ACTUALS' AND period = %(p)s",
        parameters={"p": period},
    ).result_rows
    if not rows or rows[0][0] is None:
        pytest.skip(f"no rows for period={period!r}; run the upstream load first")
    total = rows[0][0]
    assert abs(float(total)) < 0.01, (
        f"GL not balanced for period={period}: sum(amount) = {total}"
    )
