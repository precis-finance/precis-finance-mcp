# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Data-quality check engine — compiler, severity/verdict, reconcile diff.

Exercises `precis_mcp.ingestion.checks.run_checks` against the shared
FakeChClient (the staged-side SQL) and in-memory source totals (the reconcile
side). The orchestrator wiring (gate → swap, reconcile capture during extract,
record_checks) is covered in test_orchestrator.py.
"""
from __future__ import annotations

import pytest

from precis_mcp.ingestion import checks as dq
from precis_mcp.ingestion.registry import Binding
from tests.factories.ingestion import make_binding
from tests.fakes.ingestion import FakeChClient


def _binding(checks: list[dict]) -> Binding:
    return Binding.model_validate({**make_binding(), "checks": checks})


def _run_checks(
    binding: Binding,
    *,
    ch_client: FakeChClient,
    reconcile_source_totals=None,
):
    return dq.run_checks(
        binding,
        ch_client=ch_client,
        period="2026-05",
        load_id="load-current",
        reconcile_source_totals=reconcile_source_totals,
    )


# --- verdict classification ------------------------------------------------


def test_no_checks_passes():
    res = _run_checks(_binding([]), ch_client=FakeChClient())
    assert res.verdict == dq.VERDICT_PASSED
    assert res.blocked is False


def test_passing_check():
    fake = FakeChClient()
    fake.set_response("amount is null", [(0,)])
    res = _run_checks(
        _binding([{"name": "amt", "type": "not_null", "column": "amount"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_PASSED
    assert res.results[0].passed is True and res.results[0].failing == 0


def test_error_check_failing_blocks_swap():
    fake = FakeChClient()
    fake.set_response("amount is null", [(3,)])
    res = _run_checks(
        _binding([{"name": "amt", "type": "not_null", "column": "amount",
                   "severity": "error"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_FAILED
    assert res.blocked is True
    assert res.results[0].failing == 3 and res.results[0].passed is False


def test_warning_check_loads_with_warnings():
    fake = FakeChClient()
    fake.set_response("not (account_type", [(2,)])
    res = _run_checks(
        _binding([{"name": "sign", "type": "expression",
                   "expression": "NOT (account_type = 'revenue' AND amount < 0)",
                   "severity": "warning"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_WARNINGS
    assert res.blocked is False


def test_info_check_records_but_never_gates():
    fake = FakeChClient()
    fake.set_response("< 1", [(1,)])  # row_count below min → trips, but info
    res = _run_checks(
        _binding([{"name": "obs", "type": "row_count", "min": 1,
                   "severity": "info"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_PASSED
    assert res.results[0].passed is False  # recorded as a trip, doesn't gate


def test_error_beats_warning_in_verdict():
    fake = FakeChClient()
    fake.set_response("amount is null", [(1,)])
    fake.set_response("not (amount >= 0)", [(1,)])
    res = _run_checks(
        _binding([
            {"name": "warn", "type": "expression", "expression": "amount >= 0",
             "severity": "warning"},
            {"name": "err", "type": "not_null", "column": "amount",
             "severity": "error"},
        ]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_FAILED


# --- thresholds ------------------------------------------------------------


def test_threshold_tolerates_below_limit():
    fake = FakeChClient()
    fake.set_response("not (amount >= 0)", [(3,)])  # 3, threshold "> 5" → no trip
    res = _run_checks(
        _binding([{"name": "sign", "type": "expression", "expression": "amount >= 0",
                   "threshold": "> 5", "severity": "warning"}]),
        ch_client=fake,
    )
    assert res.results[0].passed is True
    assert res.verdict == dq.VERDICT_PASSED


def test_threshold_trips_above_limit():
    fake = FakeChClient()
    fake.set_response("not (amount >= 0)", [(7,)])  # 7, threshold "> 5" → trips
    res = _run_checks(
        _binding([{"name": "sign", "type": "expression", "expression": "amount >= 0",
                   "threshold": "> 5", "severity": "error"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_FAILED


# --- raw sql escape hatch + errored check ----------------------------------


def test_raw_sql_check():
    fake = FakeChClient()
    fake.set_response("currency not in", [(4,)])
    res = _run_checks(
        _binding([{"name": "ccy",
                   "sql": "SELECT * FROM {staging} WHERE currency NOT IN ('GBP')",
                   "severity": "error"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_FAILED
    # {staging} resolves to the exact current-attempt slice, not the whole
    # shared staging table where failed/stop-after partitions may remain.
    issued = "\n".join(fake.queries)
    assert "staging.fact_gl" in issued
    assert "period = '2026-05'" in issued
    assert "_load_id = 'load-current'" in issued


def test_errored_check_is_fail_closed():
    fake = FakeChClient()
    fake.fail_query_with = RuntimeError("bad SQL")
    res = _run_checks(
        _binding([{"name": "amt", "type": "not_null", "column": "amount",
                   "severity": "error"}]),
        ch_client=fake,
    )
    assert res.verdict == dq.VERDICT_FAILED
    assert res.results[0].error is not None and res.results[0].passed is False


# --- reconcile -------------------------------------------------------------


_RECON = {
    "name": "recon", "type": "reconcile", "group_by": ["period"],
    "measures": {"amount_sum": {"expr": "sum(amount)", "tolerance": {"abs": 0.01}}},
    "source_query": "SELECT period, SUM(amount) AS amount_sum FROM tb GROUP BY 1",
    "severity": "error",
}


def test_reconcile_within_tolerance_passes():
    fake = FakeChClient()
    fake.set_response("group by period", [("2026-05", 4210.30)])
    res = _run_checks(
        _binding([_RECON]), ch_client=fake,
        reconcile_source_totals={"recon": [{"period": "2026-05", "amount_sum": 4210.305}]},
    )
    assert res.results[0].passed is True and res.verdict == dq.VERDICT_PASSED
    staged_sql = next(q for q in fake.queries if "GROUP BY period" in q)
    assert "period = '2026-05'" in staged_sql
    assert "_load_id = 'load-current'" in staged_sql


def test_reconcile_out_of_tolerance_fails():
    fake = FakeChClient()
    fake.set_response("group by period", [("2026-05", 4210.30)])
    res = _run_checks(
        _binding([_RECON]), ch_client=fake,
        reconcile_source_totals={"recon": [{"period": "2026-05", "amount_sum": 4260.30}]},
    )
    assert res.results[0].passed is False and res.results[0].failing == 1
    assert res.verdict == dq.VERDICT_FAILED


def test_reconcile_grain_mismatch_fails():
    recon_by_account = {
        **_RECON,
        "group_by": ["account_code"],
        "source_query": (
            "SELECT account_code, SUM(amount) AS amount_sum "
            "FROM tb GROUP BY 1"
        ),
    }
    fake = FakeChClient()
    fake.set_response("group by account_code", [("1100", 100.0), ("1200", 200.0)])
    res = _run_checks(
        _binding([recon_by_account]), ch_client=fake,
        reconcile_source_totals={
            "recon": [{"account_code": "1100", "amount_sum": 100.0}],
        },
    )
    # Account 1200 present staged-side, absent source-side → unmatched group.
    assert res.results[0].failing == 1 and res.verdict == dq.VERDICT_FAILED


def test_reconcile_missing_source_totals_is_check_error():
    with pytest.raises(dq.CheckError):
        _run_checks(
            _binding([_RECON]),
            ch_client=FakeChClient(),
            reconcile_source_totals={},
        )


def test_curated_check_combines_operator_where_with_attempt_scope():
    fake = FakeChClient()
    fake.set_response("amount is null", [(0,)])
    _run_checks(
        _binding([{
            "name": "amt",
            "type": "not_null",
            "column": "amount",
            "where": "account_type = 'revenue'",
        }]),
        ch_client=fake,
    )

    issued = "\n".join(fake.queries)
    assert "period = '2026-05'" in issued
    assert "_load_id = 'load-current'" in issued
    assert "account_type = 'revenue'" in issued


def test_snapshot_check_scopes_to_load_id_without_period():
    binding = Binding.model_validate({
        **make_binding(
            binding_id="test_pg__dim_account",
            target="live.dim_account",
            kind="snapshot",
        ),
        "checks": [{
            "name": "code",
            "type": "not_null",
            "column": "account_code",
        }],
    })
    fake = FakeChClient()
    fake.set_response("account_code is null", [(0,)])

    dq.run_checks(
        binding,
        ch_client=fake,
        period=None,
        load_id="snapshot-load",
    )

    issued = "\n".join(fake.queries)
    assert "_load_id = 'snapshot-load'" in issued
    assert "period =" not in issued


# --- model validation ------------------------------------------------------


def test_check_requires_exactly_one_of_type_or_sql():
    with pytest.raises(Exception):
        _binding([{"name": "x", "type": "not_null", "column": "a", "sql": "SELECT 1"}])
    with pytest.raises(Exception):
        _binding([{"name": "x"}])


def test_check_type_required_params_enforced():
    with pytest.raises(Exception):
        _binding([{"name": "x", "type": "not_null"}])          # column missing
    with pytest.raises(Exception):
        _binding([{"name": "x", "type": "reconcile", "group_by": ["p"]}])  # measures/source_query


def test_check_threshold_format_validated():
    with pytest.raises(Exception):
        _binding([{"name": "x", "type": "not_null", "column": "a",
                   "threshold": "lots"}])


def test_duplicate_check_names_rejected():
    with pytest.raises(Exception):
        _binding([
            {"name": "dup", "type": "not_null", "column": "a"},
            {"name": "dup", "type": "not_null", "column": "b"},
        ])
