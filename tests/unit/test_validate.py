# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/validate.py`.

Pure logic: queries CH's `system.columns` for both `live.<x>` and
`staging.<x>`, compares the column maps (with whitespace-normalised
types), returns a `ValidationResult`. The structural guard fires
between extract and swap.
"""

from __future__ import annotations

import pytest

from precis_mcp.ingestion.validate import (
    ValidationError,
    ValidationResult,
    _normalise_type,
    _read_column_shape,
    validate_staging_shape,
)


# ---------------------------------------------------------------------------
# Inline stub: substring-routed query() returning canned column shapes
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, rows):
        self.result_rows = rows


class _StubCH:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self._patterns: list[tuple[str, list[tuple]]] = []

    def set_response(self, pattern: str, rows: list[tuple]) -> None:
        self._patterns.append((pattern.lower(), rows))

    def query(self, sql: str) -> _StubResult:
        self.queries.append(sql)
        sql_lower = sql.lower()
        for pattern, rows in self._patterns:
            if pattern in sql_lower:
                return _StubResult(rows)
        return _StubResult([])


_DEFAULT_COLS = [
    ("period", "String"),
    ("account_code", "String"),
    ("amount", "Decimal(18, 2)"),
]


# ---------------------------------------------------------------------------
# Happy path — shapes match
# ---------------------------------------------------------------------------


def test_validate_passes_when_shapes_match_exactly():
    ch = _StubCH()
    ch.set_response("database = 'live'", _DEFAULT_COLS)
    ch.set_response("database = 'staging'", _DEFAULT_COLS)
    result = validate_staging_shape(target="live.fact_gl", ch_client=ch)
    assert result.passed is True
    assert result.target == "live.fact_gl"
    assert result.staging_table == "staging.fact_gl"
    assert result.missing_in_staging == ()
    assert result.extra_in_staging == ()
    assert result.type_mismatches == ()


def test_validate_normalises_whitespace_in_types():
    """`Decimal(18, 2)` and `Decimal(18,2)` are the same type — the
    whitespace in CH's `system.columns.type` rendering shouldn't be
    flagged as drift."""
    ch = _StubCH()
    ch.set_response("database = 'live'", [("amount", "Decimal(18, 2)")])
    ch.set_response("database = 'staging'", [("amount", "Decimal(18,2)")])
    result = validate_staging_shape(target="live.fact_gl", ch_client=ch)
    assert result.passed is True


def test_normalise_type_strips_all_whitespace():
    assert _normalise_type("Decimal(18, 2)") == "Decimal(18,2)"
    assert _normalise_type("Nullable( String )") == "Nullable(String)"


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_validate_reports_type_mismatch_with_both_sides():
    ch = _StubCH()
    ch.set_response("database = 'live'", [("amount", "Decimal(18, 2)")])
    ch.set_response("database = 'staging'", [("amount", "Decimal(10, 2)")])
    result = validate_staging_shape(target="live.fact_gl", ch_client=ch)
    assert result.passed is False
    assert result.type_mismatches == (
        ("amount", "Decimal(18, 2)", "Decimal(10, 2)"),
    )


def test_validate_reports_column_missing_in_staging():
    ch = _StubCH()
    ch.set_response("database = 'live'", _DEFAULT_COLS)
    ch.set_response("database = 'staging'", _DEFAULT_COLS[:-1])  # amount dropped
    result = validate_staging_shape(target="live.fact_gl", ch_client=ch)
    assert result.passed is False
    assert result.missing_in_staging == ("amount",)
    assert result.extra_in_staging == ()


def test_validate_reports_column_extra_in_staging():
    ch = _StubCH()
    ch.set_response("database = 'live'", _DEFAULT_COLS)
    ch.set_response(
        "database = 'staging'",
        _DEFAULT_COLS + [("rogue", "String")],
    )
    result = validate_staging_shape(target="live.fact_gl", ch_client=ch)
    assert result.passed is False
    assert result.extra_in_staging == ("rogue",)
    assert result.missing_in_staging == ()


# ---------------------------------------------------------------------------
# ValidationError — preconditions
# ---------------------------------------------------------------------------


def test_validate_rejects_target_without_live_prefix():
    with pytest.raises(ValidationError, match="must start with 'live.'"):
        validate_staging_shape(target="warehouse.fact_gl", ch_client=_StubCH())


def test_validate_raises_when_live_table_missing():
    ch = _StubCH()
    # staging exists; live doesn't (empty result for that database).
    ch.set_response("database = 'staging'", _DEFAULT_COLS)
    with pytest.raises(ValidationError, match="Live table"):
        validate_staging_shape(target="live.fact_gl", ch_client=ch)


def test_validate_raises_when_staging_table_missing():
    ch = _StubCH()
    ch.set_response("database = 'live'", _DEFAULT_COLS)
    with pytest.raises(ValidationError, match="Staging table"):
        validate_staging_shape(target="live.fact_gl", ch_client=ch)


# ---------------------------------------------------------------------------
# ValidationResult.summary
# ---------------------------------------------------------------------------


def test_summary_passed_is_one_line():
    r = ValidationResult(
        target="live.fact_gl", staging_table="staging.fact_gl", passed=True
    )
    assert "shape OK" in r.summary()


def test_summary_failed_lists_each_drift_dimension():
    r = ValidationResult(
        target="live.fact_gl",
        staging_table="staging.fact_gl",
        passed=False,
        missing_in_staging=("foo",),
        extra_in_staging=("bar",),
        type_mismatches=(("baz", "String", "Int32"),),
    )
    out = r.summary()
    assert "foo" in out
    assert "bar" in out
    assert "baz" in out
    assert "shape drift" in out


# ---------------------------------------------------------------------------
# _read_column_shape — internal helper
# ---------------------------------------------------------------------------


def test_read_column_shape_returns_none_when_table_missing():
    ch = _StubCH()
    assert _read_column_shape(ch, "live", "absent") is None


def test_read_column_shape_returns_dict_keyed_by_name():
    ch = _StubCH()
    ch.set_response("database = 'live'", _DEFAULT_COLS)
    shape = _read_column_shape(ch, "live", "fact_gl")
    assert shape == {
        "period": "String",
        "account_code": "String",
        "amount": "Decimal(18, 2)",
    }
