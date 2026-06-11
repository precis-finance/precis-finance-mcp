# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/live_ddl_runner.py`.

Pure logic: reads .sql bodies, wraps them in
`CREATE TABLE IF NOT EXISTS <schema>.<stem> <body>` for each of two
schemas (live + staging). Tests assert on the captured SQL strings of
an inline stub `ch_client`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis_mcp.ingestion.live_ddl_runner import (
    AppliedReport,
    _read_body,
    apply_all,
)


class _StubCH:
    """Minimal ch_client stub: captures every command(sql) for assertion."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)


# ---------------------------------------------------------------------------
# apply_all happy paths
# ---------------------------------------------------------------------------


def test_apply_all_creates_each_schema(tmp_path: Path):
    ch = _StubCH()
    (tmp_path / "live").mkdir()
    apply_all(tmp_path / "live", ch)
    assert "CREATE DATABASE IF NOT EXISTS live" in ch.commands
    assert "CREATE DATABASE IF NOT EXISTS staging" in ch.commands


def test_apply_all_wraps_body_for_each_schema(tmp_path: Path):
    ch = _StubCH()
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (live_dir / "fact_gl.sql").write_text(
        "(\n    period String,\n    amount Decimal(18,2)\n)\n"
        "ENGINE = MergeTree\n"
        "PARTITION BY period\n"
        "ORDER BY period\n"
    )
    report = apply_all(live_dir, ch)

    # Each table is created in both live and staging — same body.
    table_cmds = [c for c in ch.commands if "CREATE TABLE IF NOT EXISTS" in c]
    assert len(table_cmds) == 2
    assert any("live.fact_gl" in c for c in table_cmds)
    assert any("staging.fact_gl" in c for c in table_cmds)
    # Body content propagated.
    for c in table_cmds:
        assert "period String" in c
        assert "PARTITION BY period" in c

    assert report.schemas_ensured == ["live", "staging"]
    assert ("live", "fact_gl") in report.tables_applied
    assert ("staging", "fact_gl") in report.tables_applied


def test_apply_all_empty_dir_returns_no_tables(tmp_path: Path):
    ch = _StubCH()
    empty = tmp_path / "live"
    empty.mkdir()
    report = apply_all(empty, ch)
    assert report.tables_applied == []
    # Schemas still ensured.
    assert "CREATE DATABASE IF NOT EXISTS live" in ch.commands


def test_apply_all_sorts_files_deterministically(tmp_path: Path):
    ch = _StubCH()
    d = tmp_path / "live"
    d.mkdir()
    (d / "z_table.sql").write_text("(x String) ENGINE=MergeTree ORDER BY x")
    (d / "a_table.sql").write_text("(y String) ENGINE=MergeTree ORDER BY y")
    report = apply_all(d, ch)
    applied_names = [name for _schema, name in report.tables_applied]
    # a_table applied in both schemas before z_table.
    assert applied_names == ["a_table", "a_table", "z_table", "z_table"]


def test_apply_all_propagates_ch_error(tmp_path: Path):
    class Failing(_StubCH):
        def command(self, sql: str) -> None:
            super().command(sql)
            if "CREATE TABLE" in sql:
                raise RuntimeError("CH offline")

    ch = Failing()
    d = tmp_path / "live"
    d.mkdir()
    (d / "fact_gl.sql").write_text("(x String) ENGINE=MergeTree ORDER BY x")
    with pytest.raises(RuntimeError, match="CH offline"):
        apply_all(d, ch)


# ---------------------------------------------------------------------------
# _read_body — comment + semicolon stripping
# ---------------------------------------------------------------------------


def test_read_body_strips_leading_comment_block(tmp_path: Path):
    f = tmp_path / "x.sql"
    f.write_text(
        "-- comment 1\n"
        "-- comment 2\n"
        "\n"
        "(period String)\n"
        "ENGINE = MergeTree\n"
        "ORDER BY period"
    )
    body = _read_body(f)
    assert body.startswith("(period String)")
    assert "comment" not in body


def test_read_body_strips_trailing_semicolon(tmp_path: Path):
    f = tmp_path / "x.sql"
    f.write_text("(x String) ENGINE=MergeTree ORDER BY x;")
    body = _read_body(f)
    assert not body.endswith(";")


def test_read_body_preserves_inline_comments_after_first_token(tmp_path: Path):
    """Comments mid-body are passed through — ClickHouse accepts `--`
    line comments between SQL tokens."""
    f = tmp_path / "x.sql"
    f.write_text(
        "(\n"
        "    period String,  -- accounting period\n"
        "    amount Decimal(18,2)\n"
        ")\n"
        "ENGINE = MergeTree\n"
        "ORDER BY period"
    )
    body = _read_body(f)
    assert "-- accounting period" in body


def test_applied_report_default_lists_empty():
    r = AppliedReport()
    assert r.schemas_ensured == []
    assert r.tables_applied == []
