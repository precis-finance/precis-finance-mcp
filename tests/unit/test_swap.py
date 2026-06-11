# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/swap.py`.

Pure logic: render the kind-appropriate SQL statement (REPLACE
PARTITION for period bindings, EXCHANGE TABLES for snapshot), then
issue it via the CH client. Period bindings additionally fire DROP
PARTITION on staging post-swap.
"""

from __future__ import annotations

import pytest

from precis_mcp.ingestion.swap import (
    SwapError,
    render_exchange_sql,
    render_replace_partition_sql,
    swap,
)


class _StubCH:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)


# ---------------------------------------------------------------------------
# Renderers — exposed for testing without a CH client
# ---------------------------------------------------------------------------


def test_render_replace_partition_quotes_period_literal():
    sql = render_replace_partition_sql("live.fact_gl", "2026-04")
    assert sql == (
        "ALTER TABLE live.fact_gl REPLACE PARTITION '2026-04' "
        "FROM staging.fact_gl"
    )


def test_render_replace_partition_handles_adjustment_period():
    sql = render_replace_partition_sql("live.fact_gl", "2026-12-ADJ")
    assert "'2026-12-ADJ'" in sql


def test_render_exchange_pairs_live_and_staging():
    sql = render_exchange_sql("live.dim_client")
    assert sql == "EXCHANGE TABLES live.dim_client AND staging.dim_client"


# ---------------------------------------------------------------------------
# swap() — period dispatch
# ---------------------------------------------------------------------------


def test_swap_period_fires_replace_partition_then_drop_staging():
    """Per-period atomic: REPLACE PARTITION promotes the slice; the
    follow-up DROP PARTITION clears staging so the next load doesn't
    accumulate. CH treats DROP on a non-existent partition as a no-op."""
    ch = _StubCH()
    swap(
        kind="period",
        period="2026-04",
        target="live.fact_gl",
        load_id="L1",
        ch_client=ch,
    )
    assert len(ch.commands) == 2
    assert "REPLACE PARTITION '2026-04'" in ch.commands[0]
    assert "live.fact_gl" in ch.commands[0]
    assert "staging.fact_gl" in ch.commands[0]
    assert ch.commands[1] == (
        "ALTER TABLE staging.fact_gl DROP PARTITION '2026-04'"
    )


def test_swap_period_requires_period():
    with pytest.raises(SwapError, match="kind='period'"):
        swap(
            kind="period",
            period=None,
            target="live.fact_gl",
            load_id="L1",
            ch_client=_StubCH(),
        )


# ---------------------------------------------------------------------------
# swap() — snapshot dispatch
# ---------------------------------------------------------------------------


def test_swap_snapshot_fires_exchange_tables_no_followup():
    """EXCHANGE TABLES is metadata-only; staging holds the previous
    snapshot afterward, and the next ibis_executor TRUNCATEs before
    INSERT, so no cleanup is needed here."""
    ch = _StubCH()
    swap(
        kind="snapshot",
        period=None,
        target="live.dim_client",
        load_id="L1",
        ch_client=ch,
    )
    assert ch.commands == [
        "EXCHANGE TABLES live.dim_client AND staging.dim_client"
    ]


def test_swap_snapshot_rejects_period():
    with pytest.raises(SwapError, match="kind='snapshot'"):
        swap(
            kind="snapshot",
            period="2026-04",
            target="live.dim_client",
            load_id="L1",
            ch_client=_StubCH(),
        )


# ---------------------------------------------------------------------------
# swap() — argument validation
# ---------------------------------------------------------------------------


def test_swap_rejects_target_without_live_prefix():
    with pytest.raises(SwapError, match="must start with 'live.'"):
        swap(
            kind="period",
            period="2026-04",
            target="warehouse.fact_gl",
            load_id="L1",
            ch_client=_StubCH(),
        )


def test_swap_rejects_unknown_kind():
    with pytest.raises(SwapError, match="must be 'period' or 'snapshot'"):
        swap(
            kind="incremental",
            period="2026-04",
            target="live.fact_gl",
            load_id="L1",
            ch_client=_StubCH(),
        )


def test_swap_propagates_ch_command_failure():
    """Swap doesn't catch CH errors — the orchestrator translates to
    `failed_swap` based on the propagated exception."""

    class _FailingCH:
        def command(self, _sql: str) -> None:
            raise RuntimeError("ClickHouse offline")

    with pytest.raises(RuntimeError, match="ClickHouse offline"):
        swap(
            kind="period",
            period="2026-04",
            target="live.fact_gl",
            load_id="L1",
            ch_client=_FailingCH(),
        )
