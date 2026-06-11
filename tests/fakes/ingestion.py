# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-memory fakes for ingestion tests.

Replaces the real `LoadHistoryWriter`, Redis-backed lock, ClickHouse
client, and Ibis backend with in-process classes that record calls and
accept canned responses. Tests pull these in via
`from tests.fakes.ingestion import …`.

The fakes are shared infrastructure — component tests under
`tests/component/` import these directly; unit tests under
`tests/unit/` should roll their own one-shot stubs inline (the
`tests/fakes/` package leans on libraries — `pandas`, `pydantic` — that
are fine for component but pull more weight than a unit test should
carry).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


__all__ = [
    "FakeLoadHistoryWriter",
    "FakeLock",
    "FakeLockFactory",
    "FakeChClient",
    "FakeIbisBackend",
]


# ---------------------------------------------------------------------------
# LoadHistoryWriter
# ---------------------------------------------------------------------------


class FakeLoadHistoryWriter:
    """In-memory substitute for the Postgres-backed `LoadHistoryWriter`.

    Records every state transition (insert / record_extract / record_swap
    / finalise) so tests can assert on the exact event order. `clock` is
    an optional callable returning the timestamp `insert_attempt`
    stamps; defaults to wall-clock UTC.
    """

    def __init__(self, *, clock=None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def insert_attempt(
        self,
        load_id,
        binding_id,
        source_id,
        dataset_id,
        period,
        scenario_id,
        triggered_by,
        notes=None,
    ) -> None:
        self.rows[load_id] = {
            "load_id": load_id,
            "binding_id": binding_id,
            "source_id": source_id,
            "dataset_id": dataset_id,
            "period": period,
            "scenario_id": scenario_id,
            "triggered_by": triggered_by,
            "notes": notes,
            "status": "running",
            "started_at": self._clock(),
        }
        self.events.append(f"insert:{load_id}")

    def record_extract(self, load_id, rows_landed, source_manifest) -> None:
        self.rows[load_id]["rows_landed"] = rows_landed
        self.rows[load_id]["source_manifest"] = source_manifest
        self.events.append(f"extract:{load_id}")

    def record_swap(self, load_id) -> None:
        self.rows[load_id]["swap_committed_at"] = "now"
        self.events.append(f"swap:{load_id}")

    def finalise(self, load_id, status, error_message=None) -> None:
        self.rows[load_id]["status"] = status
        self.rows[load_id]["finished_at"] = "now"
        self.rows[load_id]["error_message"] = error_message
        self.events.append(f"final:{load_id}:{status}")

    def find_running_for_binding(self, binding_id) -> Optional[str]:
        for lid, row in self.rows.items():
            if row["binding_id"] == binding_id and row["status"] == "running":
                return lid
        return None

    # -- Watcher + scheduler hooks ----------------------------------------

    def processed_watch_keys_for_binding(self, binding_id) -> set[str]:
        """File keys this binding has previously attempted via the watcher
        trigger path. Mirrors the Postgres implementation that scans
        `triggered_by LIKE 'watch:%'` rows."""
        out: set[str] = set()
        for row in self.rows.values():
            if row["binding_id"] != binding_id:
                continue
            tb = row.get("triggered_by", "")
            if tb.startswith("watch:"):
                out.add(tb[len("watch:"):])
        return out

    def last_scheduled_attempt_at(self, binding_id):
        """`started_at` of the most recent `schedule:*`-triggered attempt
        on this binding, or None."""
        candidates = []
        for row in self.rows.values():
            if row["binding_id"] != binding_id:
                continue
            tb = row.get("triggered_by", "")
            if not tb.startswith("schedule:"):
                continue
            started = row.get("started_at")
            if started is not None:
                candidates.append(started)
        if not candidates:
            return None
        return sorted(candidates)[-1]

    def latest_successful_period(self, binding_id) -> Optional[str]:
        """Highest `period` from successful loads for this binding, or None."""
        periods = [
            row["period"]
            for row in self.rows.values()
            if row["binding_id"] == binding_id and row["status"] == "success"
        ]
        return max(periods) if periods else None


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class FakeLock:
    """Single-lock state machine with manual control over availability."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.acquired_count = 0
        self.released_count = 0

    def acquire(self, blocking: bool = True) -> bool:
        if not self.available:
            return False
        self.acquired_count += 1
        return True

    def release(self) -> None:
        self.released_count += 1


class FakeLockFactory:
    """Callable that produces `FakeLock` instances and records call signatures."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, int, int]] = []
        self.last_lock: Optional[FakeLock] = None

    def __call__(
        self, key: str, *, timeout: int, blocking_timeout: int
    ) -> FakeLock:
        self.calls.append((key, timeout, blocking_timeout))
        self.last_lock = FakeLock(available=self.available)
        return self.last_lock


# ---------------------------------------------------------------------------
# ClickHouse client
# ---------------------------------------------------------------------------


class _FakeQueryResult:
    """Mimics clickhouse-connect's `QueryResult`: `.result_rows` and
    `.column_names`."""

    def __init__(
        self,
        rows: list[tuple],
        column_names: Optional[list[str]] = None,
    ) -> None:
        self.result_rows = rows
        self.column_names = column_names or []


class FakeChClient:
    """ClickHouse-connect client substitute.

    - `.command(sql)` is captured into `commands`. DDL/DML — fire-and-forget.
    - `.query(sql)` returns a `_FakeQueryResult`. Responses routed by
      substring match against the SQL via `set_response(pattern, rows)`
      or `set_query_responses(*results)` FIFO. Default empty result.
    - `.insert_df(table, df)` captures into `inserts`. The Ibis executor
      uses this; tests can assert on the inserted DataFrame.

    Failures: set `.fail_command_with` / `.fail_query_with` to an
    Exception instance to make the corresponding method raise. Set
    `.fail_command_matching` to a case-insensitive SQL substring to scope
    the command failure to one stage (e.g. `"REPLACE PARTITION"` to fail
    only the swap, leaving extract's landing DDL intact).
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.queries: list[str] = []
        self.inserts: list[tuple[str, Any]] = []  # (table, df)
        self._patterns: list[tuple[str, list[tuple]]] = []
        self._fifo_responses: list[_FakeQueryResult] = []
        self.fail_command_with: Optional[Exception] = None
        self.fail_command_matching: Optional[str] = None
        self.fail_query_with: Optional[Exception] = None

    def set_response(self, pattern: str, rows: list[tuple]) -> None:
        """Route any `.query()` whose SQL contains `pattern` (case-
        insensitive) to `rows`. First match wins."""
        self._patterns.append((pattern.lower(), rows))

    def set_query_responses(self, *results: _FakeQueryResult) -> None:
        """Set a FIFO sequence of responses returned in order by
        successive `.query()` calls — overrides pattern matching."""
        self._fifo_responses = list(results)

    def command(self, sql: str) -> None:
        self.commands.append(sql)
        if self.fail_command_with is not None and (
            self.fail_command_matching is None
            or self.fail_command_matching.lower() in sql.lower()
        ):
            raise self.fail_command_with

    def query(self, sql: str) -> _FakeQueryResult:
        self.queries.append(sql)
        if self.fail_query_with is not None:
            raise self.fail_query_with
        if self._fifo_responses:
            return self._fifo_responses.pop(0)
        sql_lower = sql.lower()
        for pattern, rows in self._patterns:
            if pattern in sql_lower:
                return _FakeQueryResult(rows)
        return _FakeQueryResult([])

    def insert_df(self, table: str, df: Any, **kwargs: Any) -> None:
        self.inserts.append((table, df))


# ---------------------------------------------------------------------------
# Ibis backend
# ---------------------------------------------------------------------------


class _FakeIbisExpr:
    """Minimal substitute for `ibis.Expr` — just enough to support
    `.execute()` returning a pandas DataFrame."""

    def __init__(self, df: Any) -> None:
        self._df = df

    def execute(self) -> Any:
        return self._df


class FakeIbisBackend:
    """In-memory Ibis backend substitute.

    The executor calls `backend.sql(query_string).execute()` and expects
    a pandas DataFrame back. The fake records the query string and
    returns whatever DataFrame the test seeded.

    Pass `df=` to set the canned return value; pass `exc=` to make
    `.sql(...)` raise instead.
    """

    def __init__(
        self,
        df: Any = None,
        *,
        exc: Optional[Exception] = None,
    ) -> None:
        self.queries: list[str] = []
        self._df = df
        self._exc = exc

    def sql(self, query: str) -> _FakeIbisExpr:
        self.queries.append(query)
        if self._exc is not None:
            raise self._exc
        # Default to an empty DataFrame when no canned response is set.
        if self._df is None:
            import pandas as pd

            return _FakeIbisExpr(pd.DataFrame())
        return _FakeIbisExpr(self._df)
