# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""CRM synthetic-data generator — schema and distribution coverage.

Exercises the `generate_crm_accounts` and `generate_crm_opportunities` functions
in `precis_mcp/sample_data/generate.py`. The generator writes CSVs the
file-drop ingestion path consumes for the demo / smoke-test pipeline; this
test pins the row shapes and distribution properties the rest of the stack
relies on.

Component-class because the generator imports `psycopg` and
`clickhouse_connect` at module level — `clickhouse_connect` is rejected by
the unit conftest I/O guard at collection time. The module's import-time work
is benign (builds DSN dicts and constants only; no real connections opened).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def synth_module() -> ModuleType:
    """Import the generator once for the module's tests.

    Module-level side effects: reads .env if present, sets DSN dicts and
    constants, seeds RNGs. No external connection is opened.
    """
    from precis_mcp.sample_data import generate

    return generate


def test_crm_accounts_row_count_and_schema(synth_module):
    accounts = synth_module.generate_crm_accounts()

    assert len(accounts) == synth_module.CRM_NUM_ACCOUNTS == 50

    expected_keys = {
        "account_id", "account_name", "industry", "region", "segment",
        "created_date",
    }
    for acc in accounts:
        assert set(acc.keys()) == expected_keys

    # Account IDs are unique.
    assert len({a["account_id"] for a in accounts}) == len(accounts)

    # Segments fall within the declared bucket set.
    seg_values = {a["segment"] for a in accounts}
    assert seg_values.issubset(set(synth_module.CRM_SEGMENTS))


def test_crm_opportunities_row_count_and_schema(synth_module):
    accounts = synth_module.generate_crm_accounts()
    opps = synth_module.generate_crm_opportunities(accounts)

    assert len(opps) == synth_module.CRM_NUM_OPPORTUNITIES == 300

    expected_keys = {
        "opportunity_id", "account_id", "opportunity_name", "stage",
        "stage_category", "probability", "amount", "currency",
        "created_date", "close_date", "last_stage_change_date", "owner",
        "service_line", "engagement_type", "duration_months",
        "estimated_start_date", "source",
    }
    for opp in opps:
        assert set(opp.keys()) == expected_keys

    # Opp IDs are unique.
    assert len({o["opportunity_id"] for o in opps}) == len(opps)


def test_crm_opportunities_status_mix(synth_module):
    accounts = synth_module.generate_crm_accounts()
    opps = synth_module.generate_crm_opportunities(accounts)
    total = len(opps)

    mix = Counter(o["stage_category"] for o in opps)
    # Target distribution: Open ~60%, Won ~25%, Lost ~15%. Tolerance bands
    # absorb the rounding from the cyclic CRM_STATUS_MIX indexing.
    assert 0.50 < mix["Open"] / total < 0.70, f"Open share off: {mix}"
    assert 0.20 < mix["Won"] / total < 0.30, f"Won share off: {mix}"
    assert 0.10 < mix["Lost"] / total < 0.20, f"Lost share off: {mix}"


def test_crm_opportunities_probability_matches_stage(synth_module):
    accounts = synth_module.generate_crm_accounts()
    opps = synth_module.generate_crm_opportunities(accounts)
    stage_prob = synth_module.CRM_STAGE_PROB

    for opp in opps:
        # The generator formats probability as a fixed-precision string;
        # parse back to float for the comparison.
        actual = float(opp["probability"])
        expected = stage_prob[opp["stage"]]
        assert actual == pytest.approx(expected), (
            f"probability {actual} does not match stage {opp['stage']!r} "
            f"(expected {expected})"
        )


def test_crm_opportunities_account_ids_reference_existing_accounts(synth_module):
    accounts = synth_module.generate_crm_accounts()
    opps = synth_module.generate_crm_opportunities(accounts)

    account_ids = {a["account_id"] for a in accounts}
    for opp in opps:
        assert opp["account_id"] in account_ids, (
            f"opportunity {opp['opportunity_id']} references unknown "
            f"account_id={opp['account_id']!r}"
        )


# ---------------------------------------------------------------------------
# Ingestion-trigger orchestration
# ---------------------------------------------------------------------------

from dataclasses import dataclass

from precis_mcp.ingestion.registry import IntegrationRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = PROJECT_ROOT / "instance" / "integrations"


@dataclass
class _FakeResult:
    """Stand-in for orchestrator.LoadAttemptResult — only the `status` field
    is consulted by drive_bindings."""

    status: str = "success"


@pytest.fixture(scope="module")
def production_registry():
    return IntegrationRegistry.load(INTEGRATIONS_ROOT)


def _two_period_provider(binding):
    """Trivial provider for orchestration-loop tests: snapshot bindings get
    `[None]`, period bindings get a fixed two-period list. Decouples the
    fan-out assertions from any specific period-enumeration policy."""
    if binding.kind == "snapshot":
        return [None]
    return ["2025-11", "2025-12"]


def test_drive_bindings_calls_run_binding_once_per_binding_period_pair(
    synth_module, production_registry,
):
    calls: list[tuple[str, object, str]] = []

    def fake_run_binding(_ctx, binding_id, period, *, triggered_by):
        calls.append((binding_id, period, triggered_by))
        return _FakeResult(status="success")

    total_ok, total_failed = synth_module.drive_bindings(
        production_registry, ctx=None,
        period_provider_fn=_two_period_provider,
        run_binding_fn=fake_run_binding,
    )

    expected_total = sum(
        len(_two_period_provider(b))
        for b in production_registry.bindings.values()
    )
    assert len(calls) == expected_total
    assert total_ok == expected_total
    assert total_failed == 0


def test_drive_bindings_uses_synthetic_data_bootstrap_triggered_by(
    synth_module, production_registry,
):
    seen: list[str] = []

    def fake_run_binding(_ctx, _binding_id, _period, *, triggered_by):
        seen.append(triggered_by)
        return _FakeResult(status="success")

    synth_module.drive_bindings(
        production_registry, ctx=None,
        period_provider_fn=_two_period_provider,
        run_binding_fn=fake_run_binding,
    )

    # Every call carries the audit suffix the bootstrap is the source of.
    assert seen, "no run_binding calls captured"
    assert set(seen) == {"synthetic_data_bootstrap"}
    assert synth_module.INGESTION_TRIGGERED_BY == "synthetic_data_bootstrap"


def test_drive_bindings_counts_failures_and_continues(
    synth_module, production_registry,
):
    """One binding raises; the rest succeed. Loop continues past the failure
    rather than aborting, and the failure is counted in the summary."""

    failing_binding = "customer_pg__gl"

    def fake_run_binding(_ctx, binding_id, _period, *, triggered_by):
        del triggered_by  # signature parity with the real run_binding
        if binding_id == failing_binding:
            raise RuntimeError("simulated driver crash")
        return _FakeResult(status="success")

    total_ok, total_failed = synth_module.drive_bindings(
        production_registry, ctx=None,
        period_provider_fn=_two_period_provider,
        run_binding_fn=fake_run_binding,
    )

    failed_periods = len(
        _two_period_provider(production_registry.bindings[failing_binding])
    )
    assert total_failed == failed_periods
    assert total_ok == (
        sum(
            len(_two_period_provider(b))
            for b in production_registry.bindings.values()
        ) - failed_periods
    )


def test_drive_bindings_counts_non_success_status_as_failed(
    synth_module, production_registry,
):
    """run_binding can also indicate failure via status != 'success' without
    raising — make sure that path is counted as a failure."""

    def fake_run_binding(_ctx, _binding_id, _period, *, triggered_by):
        del triggered_by
        return _FakeResult(status="failed_recon")

    total_ok, total_failed = synth_module.drive_bindings(
        production_registry, ctx=None,
        period_provider_fn=_two_period_provider,
        run_binding_fn=fake_run_binding,
    )

    assert total_ok == 0
    assert total_failed > 0


def test_synth_period_provider_snapshot_bindings_return_none(
    synth_module, production_registry,
):
    """Snapshot bindings have no period axis — the synth provider hands the
    orchestrator a single `None` so `run_binding` falls through to its
    snapshot-stamping branch."""

    snapshot_bindings = [
        b for b in production_registry.bindings.values()
        if b.kind == "snapshot"
    ]
    assert snapshot_bindings, "registry has no snapshot bindings to exercise"
    for b in snapshot_bindings:
        assert synth_module.synth_period_provider(b) == [None]


def test_synth_period_provider_period_bindings_cover_full_actuals_span(
    synth_module, production_registry,
):
    """Period bindings get the full generated actuals history, regardless of
    their own `period_selection` (the BAU lookback would under-load the
    bootstrap). Lower/upper bounds are pinned to ACTUALS_START / ACTUALS_END."""

    period_bindings = [
        b for b in production_registry.bindings.values()
        if b.kind == "period"
    ]
    assert period_bindings, "registry has no period bindings to exercise"

    expected_first = synth_module.period_str(
        synth_module.ACTUALS_START.year, synth_module.ACTUALS_START.month,
    )
    expected_last = synth_module.period_str(
        synth_module.ACTUALS_END.year, synth_module.ACTUALS_END.month,
    )

    for b in period_bindings:
        periods = synth_module.synth_period_provider(b)
        assert periods[0] == expected_first
        assert periods[-1] == expected_last
        # Strictly monotonic month-by-month — no gaps, no duplicates.
        assert periods == sorted(set(periods))
        # Spans the full inclusive month range.
        months = (
            (synth_module.ACTUALS_END.year - synth_module.ACTUALS_START.year) * 12
            + (synth_module.ACTUALS_END.month - synth_module.ACTUALS_START.month)
            + 1
        )
        assert len(periods) == months
