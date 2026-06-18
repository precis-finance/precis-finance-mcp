# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-memory ClickHouse client substitute.

Replaces the inline `patch("…get_clickhouse_client")` + `MagicMock` pattern
that currently lives in 14+ test files. The real client returned by
`precis_mcp.db.get_clickhouse_client()` is a `clickhouse_connect.driver.client.Client`;
this fake mirrors the subset of that surface production code actually uses
(`.query`, `.command`, `.insert`).

Four usage modes; see `FakeClickHouseClient.query` for resolution order:

1. **Empty default** — every `.query()` returns an empty `FakeQueryResult`.
2. **`response_map={substring: rows}`** — `.query(sql)` returns the first
   entry whose key is a case-insensitive substring of `sql`. Useful when one
   test exercises several distinct queries.
3. **`.push(rows, column_names=None)`** — append a per-call response to a
   FIFO queue. Useful when the same SQL is run multiple times with different
   expected returns.
4. **`.set_query_responses(*results)`** — *replace* the FIFO queue with the
   given pre-built `FakeQueryResult` instances (or row-list shorthands).
   Resets the queue cursor. Useful when a test scripts a fresh, fixed
   sequence of returns up front.

The fake records every `.query()`, `.command()`, and `.insert()` call so
tests can assert on the SQL, the parameter bindings, or the inserted
payload. `.commands` is a list of `(sql, parameters)` tuples — parameters
is whatever the caller passed (often `None` for parameterless DDL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


__all__ = ["FakeQueryResult", "FakeClickHouseClient"]


@dataclass
class FakeQueryResult:
    """The shape production code reads back from a ClickHouse `.query()` call.

    Mirrors the attributes actually used in `precis/`: `result_rows` and
    `column_names`, plus a `named_results()` convenience that zips them into
    dicts.
    """

    result_rows: list[tuple] = field(default_factory=list)
    column_names: list[str] = field(default_factory=list)

    def named_results(self) -> list[dict[str, Any]]:
        # If column_names is empty, zip yields empty dicts — matches the
        # clickhouse-connect behaviour when no schema is attached.
        return [dict(zip(self.column_names, row)) for row in self.result_rows]


class FakeClickHouseClient:
    """Drop-in substitute for `clickhouse_connect.driver.client.Client`.

    Resolution order for `.query(sql, parameters=None)`:
      1. `response_map` substring match (case-insensitive).
      2. FIFO queue populated by `.push(...)` or `.set_query_responses(...)`.
      3. Empty `FakeQueryResult`.
    """

    def __init__(
        self,
        *,
        response_map: dict[str, list[tuple] | FakeQueryResult] | None = None,
    ) -> None:
        self.response_map: dict[str, list[tuple] | FakeQueryResult] = (
            dict(response_map) if response_map is not None else {}
        )
        self._queue: list[FakeQueryResult] = []
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.commands: list[tuple[str, dict[str, Any] | None]] = []
        # Parallel to ``commands``: the ``settings`` kwarg per command call
        # (None when the caller passed none).
        self.command_settings: list[dict[str, Any] | None] = []
        self.inserts: list[tuple[str, Any, tuple, dict[str, Any]]] = []

    # -- programmatic setup ------------------------------------------------

    def push(
        self,
        *rows: tuple | list,
        column_names: list[str] | None = None,
    ) -> "FakeClickHouseClient":
        """Append a canned response to the FIFO queue. Returns self for chaining.

        Three call shapes are accepted:

          - `push()`                          — empty result (zero rows).
          - `push((1, "a"), (2, "b"))`        — variadic row tuples.
          - `push([(1, "a"), (2, "b")])`      — single list of row tuples.
        """
        if len(rows) == 1 and isinstance(rows[0], list):
            row_list: list[tuple] = list(rows[0])
        else:
            row_list = list(rows)  # type: ignore[assignment]
        self._queue.append(
            FakeQueryResult(result_rows=row_list, column_names=column_names or [])
        )
        return self

    def set_query_responses(
        self,
        *responses: FakeQueryResult | list[tuple],
    ) -> "FakeClickHouseClient":
        """Replace the FIFO queue with a fresh scripted sequence.

        Each response may be a `FakeQueryResult` instance or a row-list
        (wrapped into a `FakeQueryResult` with empty column_names). Unlike
        `.push()`, this REPLACES the queue rather than appending — call it
        once at the top of a test to script the full `.query()` trajectory.
        """
        new_queue: list[FakeQueryResult] = []
        for r in responses:
            if isinstance(r, FakeQueryResult):
                new_queue.append(r)
            else:
                new_queue.append(FakeQueryResult(result_rows=list(r)))
        self._queue = new_queue
        return self

    def set_response(
        self,
        pattern: str,
        rows: list[tuple] | FakeQueryResult,
    ) -> "FakeClickHouseClient":
        """Add or replace a substring → rows mapping. Returns self for chaining."""
        self.response_map[pattern] = rows
        return self

    def reset(self) -> None:
        """Clear queues and recorded calls. Useful between sub-test setups."""
        self._queue.clear()
        self.queries.clear()
        self.commands.clear()
        self.command_settings.clear()
        self.inserts.clear()

    # -- ClickHouse client surface ----------------------------------------

    def query(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
    ) -> FakeQueryResult:
        self.queries.append((sql, parameters))
        sql_lower = sql.lower()
        for pattern, rows in self.response_map.items():
            if pattern.lower() in sql_lower:
                if isinstance(rows, FakeQueryResult):
                    return rows
                return FakeQueryResult(result_rows=list(rows))
        if self._queue:
            return self._queue.pop(0)
        return FakeQueryResult()

    def command(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.commands.append((sql, parameters))
        self.command_settings.append(kwargs.get("settings"))

    def insert(
        self,
        table: str,
        data: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.inserts.append((table, data, args, kwargs))

    def close(self) -> None:  # pragma: no cover — no-op in fakes
        pass
