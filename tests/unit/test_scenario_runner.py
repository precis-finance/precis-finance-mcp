# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/scenario_runner.py`.

The runner seeds `semantic.scenarios` (the package-owned scenario registry
table) from an instance-declared `scenarios.yml`. Two concerns:

  - `parse_scenarios` — pure YAML parse + validation (required fields,
    unknown fields, duplicates, defaults).
  - `apply` — CREATE schema/table, then seed-if-absent against the existing
    registry (idempotent, non-destructive).

A small hand-rolled CH stub records `.command`/`.query`/`.insert` and lets a
test script the existing-id set — same shape as `test_semantic_runner._StubCH`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

import pytest

from precis_mcp.ingestion.scenario_runner import (
    COLUMNS,
    ScenarioSeedError,
    apply,
    parse_scenarios,
)


@dataclass
class _Result:
    result_rows: list[tuple]


class _StubCH:
    """Records commands/inserts; answers the existing-ids query from a seed."""

    def __init__(self, existing_ids: tuple[str, ...] = ()) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple] = []
        self._existing = list(existing_ids)

    def command(self, sql: str, *args, **kwargs) -> None:
        self.commands.append(sql)

    def query(self, sql: str, parameters=None) -> _Result:
        return _Result(result_rows=[(i,) for i in self._existing])

    def insert(self, table, data, column_names=None, **kwargs) -> None:
        self.inserts.append((table, data, column_names))


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scenarios.yml"
    p.write_text(body, encoding="utf-8")
    return p


_MINIMAL = """
scenarios:
  - scenario_id: ACTUALS
    alias: actuals
    name: Actuals
    kind: ACTUAL
"""

_THREE = """
scenarios:
  - {scenario_id: ACTUALS, alias: actuals, name: Actuals, kind: ACTUAL, status: LOCKED}
  - {scenario_id: BUD-2026, alias: budget, name: Budget 2026, kind: BUDGET, base_scenario: ACTUALS}
  - {scenario_id: FC-2026-Q1, alias: forecast_q1, name: FC Q1, kind: FORECAST}
"""


# -- parse_scenarios ---------------------------------------------------------

def test_parse_applies_defaults(tmp_path: Path):
    [row] = parse_scenarios(_write(tmp_path, _MINIMAL))
    assert set(row) == set(COLUMNS)
    assert row["scenario_id"] == "ACTUALS"
    assert row["status"] == "DRAFT"
    assert row["granularity"] == "monthly"
    assert row["locks"] == "[]"
    assert row["created_by"] == "system"
    assert row["base_scenario"] is None
    assert row["locked_at"] is None
    # created_at / updated_at default to a real datetime (now at parse time).
    assert isinstance(row["created_at"], datetime.datetime)
    assert isinstance(row["updated_at"], datetime.datetime)


def test_parse_accepts_bare_list(tmp_path: Path):
    rows = parse_scenarios(_write(tmp_path, _MINIMAL.replace("scenarios:\n", "")))
    assert [r["scenario_id"] for r in rows] == ["ACTUALS"]


def test_parse_coerces_yaml_timestamp(tmp_path: Path):
    body = (
        "scenarios:\n"
        "  - scenario_id: ACTUALS\n"
        "    alias: actuals\n"
        "    name: Actuals\n"
        "    kind: ACTUAL\n"
        "    created_at: 2022-12-01 00:00:00\n"
        "    locked_at: 2022-12-31 00:00:00\n"
    )
    [row] = parse_scenarios(_write(tmp_path, body))
    assert row["created_at"] == datetime.datetime(2022, 12, 1)
    assert row["locked_at"] == datetime.datetime(2022, 12, 31)


def test_parse_rejects_unknown_field(tmp_path: Path):
    body = _MINIMAL + "    flavour: vanilla\n"
    with pytest.raises(ScenarioSeedError) as exc:
        parse_scenarios(_write(tmp_path, body))
    assert "flavour" in str(exc.value)


@pytest.mark.parametrize("field_", ["scenario_id", "alias", "name", "kind"])
def test_parse_rejects_missing_required(tmp_path: Path, field_: str):
    base = {"scenario_id": "X", "alias": "a", "name": "A", "kind": "BUDGET"}
    del base[field_]
    inner = ", ".join(f"{k}: {v}" for k, v in base.items())
    body = f"scenarios:\n  - {{{inner}}}\n"
    with pytest.raises(ScenarioSeedError) as exc:
        parse_scenarios(_write(tmp_path, body))
    assert field_ in str(exc.value)


def test_parse_rejects_duplicate_scenario_id(tmp_path: Path):
    body = (
        "scenarios:\n"
        "  - {scenario_id: X, alias: a, name: A, kind: BUDGET}\n"
        "  - {scenario_id: X, alias: b, name: B, kind: BUDGET}\n"
    )
    with pytest.raises(ScenarioSeedError, match="duplicate scenario_id"):
        parse_scenarios(_write(tmp_path, body))


def test_parse_rejects_duplicate_alias(tmp_path: Path):
    body = (
        "scenarios:\n"
        "  - {scenario_id: X, alias: dup, name: A, kind: BUDGET}\n"
        "  - {scenario_id: Y, alias: dup, name: B, kind: BUDGET}\n"
    )
    with pytest.raises(ScenarioSeedError, match="duplicate alias"):
        parse_scenarios(_write(tmp_path, body))


def test_parse_missing_file_raises(tmp_path: Path):
    with pytest.raises(ScenarioSeedError, match="not found"):
        parse_scenarios(tmp_path / "nope.yml")


# -- apply -------------------------------------------------------------------

def test_apply_ensures_schema_and_table(tmp_path: Path):
    ch = _StubCH()
    report = apply(_write(tmp_path, _MINIMAL), ch)
    assert any("CREATE DATABASE IF NOT EXISTS semantic" in c for c in ch.commands)
    table_cmd = next(c for c in ch.commands if "semantic.scenarios" in c)
    assert table_cmd.startswith("CREATE TABLE IF NOT EXISTS semantic.scenarios")
    assert report.schema_ensured and report.table_ensured


def test_apply_seeds_all_when_registry_empty(tmp_path: Path):
    ch = _StubCH(existing_ids=())
    report = apply(_write(tmp_path, _THREE), ch)
    assert report.seeded == ["ACTUALS", "BUD-2026", "FC-2026-Q1"]
    assert report.skipped == []
    [(table, data, column_names)] = ch.inserts
    assert table == "semantic.scenarios"
    # The insert names every package-owned column, in contract order.
    assert column_names == list(COLUMNS)
    assert len(data) == 3
    assert len(data[0]) == len(COLUMNS)


def test_apply_skips_existing_scenarios(tmp_path: Path):
    ch = _StubCH(existing_ids=("ACTUALS",))
    report = apply(_write(tmp_path, _THREE), ch)
    assert report.skipped == ["ACTUALS"]
    assert report.seeded == ["BUD-2026", "FC-2026-Q1"]
    [(_, data, _cols)] = ch.inserts
    seeded_ids = [row[0] for row in data]
    assert "ACTUALS" not in seeded_ids


def test_apply_idempotent_when_all_present(tmp_path: Path):
    ch = _StubCH(existing_ids=("ACTUALS", "BUD-2026", "FC-2026-Q1"))
    report = apply(_write(tmp_path, _THREE), ch)
    assert report.seeded == []
    assert report.skipped == ["ACTUALS", "BUD-2026", "FC-2026-Q1"]
    # Nothing to insert — no destructive re-write of existing governance state.
    assert ch.inserts == []
