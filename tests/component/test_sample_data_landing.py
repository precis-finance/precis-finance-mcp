# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-tier plan landing for the sample-data generator.

`land_plan_fact_plan` collapses the generated delta entries (the forecast is
budget rows plus adjustment deltas) into one static amount per
(period, account, cost_centre, scenario) and lands them in `live.fact_plan` —
the table the open instance's semantic views read plan from. Component-class:
exercises the lander against the fake ClickHouse client plus the live-DDL
runner over the real instance directory.
"""

from __future__ import annotations

import datetime

from precis_mcp.sample_data.generate import PLAN_LOAD_ID, land_plan_fact_plan

NOW = datetime.datetime(2026, 6, 1, 0, 0, 0)


def _entry(scenario: str, delta: float, account: str = "4100",
           cost_centre: str = "CC-100", period: str = "2026-01") -> dict:
    return {
        "account": account,
        "cost_centre": cost_centre,
        "period": period,
        "scenario": scenario,
        "delta_amount": delta,
        "user_id": "system",
        "inserted_at": NOW,
    }


def test_deltas_collapse_to_static_amounts(ch_client):
    entries = [
        _entry("BUD-2026", -1000.0),            # budget revenue (credit)
        _entry("FC-2026-Q1", -1000.0),          # forecast copy of budget row
        _entry("FC-2026-Q1", -25.5),            # forecast adjustment delta
        _entry("BUD-2026", 400.0, account="5100"),
    ]

    land_plan_fact_plan(ch_client, entries)

    assert len(ch_client.inserts) == 1
    table, rows, _args, kwargs = ch_client.inserts[0]
    assert table == "live.fact_plan"
    assert kwargs["column_names"] == [
        "period", "account_code", "cost_centre", "scenario",
        "amount", "_load_id",
    ]

    by_key = {(r[0], r[1], r[2], r[3]): r[4] for r in rows}
    # Forecast = budget copy + adjustment, collapsed to one static row.
    assert by_key[("2026-01", "4100", "CC-100", "FC-2026-Q1")] == -1025.5
    assert by_key[("2026-01", "4100", "CC-100", "BUD-2026")] == -1000.0
    assert by_key[("2026-01", "5100", "CC-100", "BUD-2026")] == 400.0
    assert all(r[5] == PLAN_LOAD_ID for r in rows)


def test_regen_truncates_before_insert(ch_client):
    land_plan_fact_plan(ch_client, [_entry("BUD-2026", 100.0)])

    truncates = [sql for sql, _ in ch_client.commands
                 if sql.strip().upper().startswith("TRUNCATE")]
    assert truncates == ["TRUNCATE TABLE live.fact_plan"]


def test_zero_sum_keys_are_dropped(ch_client):
    entries = [
        _entry("FC-2026-Q1", 500.0),
        _entry("FC-2026-Q1", -500.0),   # adjustment cancels the copy exactly
        _entry("FC-2026-Q1", 10.0, account="5100"),
    ]

    land_plan_fact_plan(ch_client, entries)

    _table, rows, _args, _kwargs = ch_client.inserts[0]
    assert [(r[1], r[4]) for r in rows] == [("5100", 10.0)]
