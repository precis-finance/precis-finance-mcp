# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""PostgresLoadHistoryWriter — concrete implementation backed by `precis_mcp.db`.

Wraps the SQL writes the orchestrator drives plus the read queries the agent
read-tools surface. Each call is its own transaction via `execute_platform` /
`query_platform`; no shared connection state across methods.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import Any, Optional

from precis_mcp import db


def _json_default(o: Any) -> Any:
    """Coerce types JSON doesn't know how to serialise. Covers the values that
    source manifests can carry: dates / datetimes from ClickHouse range
    queries, Decimals from sum-shaped manifest fields. Anything else falls
    through to a repr-based string so a single unknown type doesn't take
    the load down.
    """
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return str(o)
    return repr(o)


# ---------------------------------------------------------------------------
# Module-level read helpers — used by the MCP read tools, independent of
# any LoadHistoryWriter instance because they don't write.
# ---------------------------------------------------------------------------


_LOAD_HISTORY_COLUMNS = (
    "load_id, binding_id, source_id, dataset_id, period, scenario_id, "
    "status, triggered_by, rows_landed, source_manifest, "
    "control_total_result, started_at, finished_at, duration_ms, "
    "swap_committed_at, dbt_refreshed_at, error_message, notes"
)


def query_load_history(
    *,
    binding_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    period: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter `load_history` by the optional dims; return up to `limit` rows
    ordered most-recent-first. Used by the `list_load_history` MCP tool."""
    clauses: list[str] = []
    params: list[Any] = []
    if binding_id is not None:
        clauses.append("binding_id = %s")
        params.append(binding_id)
    if dataset_id is not None:
        clauses.append("dataset_id = %s")
        params.append(dataset_id)
    if period is not None:
        clauses.append("period = %s")
        params.append(period)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Hard upper bound to keep agent surface responsive — operator UI shows
    # paginated; agent gets the most-recent slice.
    capped = max(1, min(int(limit), 200))
    params.append(capped)

    sql = (
        f"SELECT {_LOAD_HISTORY_COLUMNS}\n"
        f"  FROM load_history\n"
        f"{where}\n"
        f" ORDER BY started_at DESC\n"
        f" LIMIT %s"
    )
    return db.query_platform(sql, tuple(params))


def get_load_history_row(load_id: str) -> Optional[dict[str, Any]]:
    """Fetch one `load_history` row by `load_id`, or None when not found.
    Used by the `get_load_status` MCP tool."""
    rows = db.query_platform(
        f"SELECT {_LOAD_HISTORY_COLUMNS}\n"
        f"  FROM load_history\n"
        f" WHERE load_id = %s\n"
        f" LIMIT 1",
        (load_id,),
    )
    return rows[0] if rows else None


class PostgresLoadHistoryWriter:
    """Default writer that hits the platform Postgres directly."""

    def insert_attempt(
        self,
        load_id: str,
        binding_id: str,
        source_id: str,
        dataset_id: str,
        period: str,
        scenario_id: str,
        triggered_by: str,
        notes: Optional[str] = None,
    ) -> None:
        db.execute_platform(
            """
            INSERT INTO load_history
                (load_id, binding_id, source_id, dataset_id, period,
                 scenario_id, status, triggered_by, notes)
            VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s)
            """,
            (
                load_id,
                binding_id,
                source_id,
                dataset_id,
                period,
                scenario_id,
                triggered_by,
                notes,
            ),
        )

    def record_extract(
        self,
        load_id: str,
        rows_landed: int,
        source_manifest: dict[str, Any],
    ) -> None:
        db.execute_platform(
            """
            UPDATE load_history
               SET rows_landed = %s,
                   source_manifest = %s::jsonb
             WHERE load_id = %s
            """,
            (rows_landed, json.dumps(source_manifest, default=_json_default), load_id),
        )

    def record_swap(self, load_id: str) -> None:
        db.execute_platform(
            """
            UPDATE load_history
               SET swap_committed_at = now()
             WHERE load_id = %s
            """,
            (load_id,),
        )

    def record_checks(
        self, load_id: str, control_total_result: dict[str, Any]
    ) -> None:
        db.execute_platform(
            """
            UPDATE load_history
               SET control_total_result = %s::jsonb
             WHERE load_id = %s
            """,
            (json.dumps(control_total_result, default=_json_default), load_id),
        )

    def record_dbt_refresh(self, load_id: str) -> None:
        db.execute_platform(
            """
            UPDATE load_history
               SET dbt_refreshed_at = now()
             WHERE load_id = %s
            """,
            (load_id,),
        )

    def finalise(
        self,
        load_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        # duration_ms is derived from started_at on the row; computing it here
        # keeps the orchestrator agnostic of clock skew between Python and PG.
        db.execute_platform(
            """
            UPDATE load_history
               SET status = %s,
                   finished_at = now(),
                   duration_ms = GREATEST(0,
                       EXTRACT(EPOCH FROM (now() - started_at)) * 1000
                   )::int,
                   error_message = %s
             WHERE load_id = %s
            """,
            (status, error_message, load_id),
        )

    def find_running_for_binding(self, binding_id: str) -> Optional[str]:
        rows = db.query_platform(
            """
            SELECT load_id
              FROM load_history
             WHERE binding_id = %s AND status = 'running'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (binding_id,),
        )
        return rows[0]["load_id"] if rows else None

    def last_scheduled_attempt_at(self, binding_id: str):
        """Return the most recent `started_at` for a `schedule:*`-triggered
        attempt on this binding, or None.

        Used by the cron scheduler to compute the next fire time on each
        tick — keeps the scheduler stateless across daemon restarts.
        """
        rows = db.query_platform(
            """
            SELECT started_at
              FROM load_history
             WHERE binding_id = %s
               AND triggered_by LIKE 'schedule:%%'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (binding_id,),
        )
        return rows[0]["started_at"] if rows else None

    def latest_successful_period(self, binding_id: str):
        """Return the highest `period` from successful loads for this binding,
        or None when no prior successful load exists.

        Watermark-strategy period selection uses this to compute the period
        list strictly after the last successful run.
        """
        rows = db.query_platform(
            """
            SELECT period
              FROM load_history
             WHERE binding_id = %s
               AND status = 'success'
             ORDER BY period DESC
             LIMIT 1
            """,
            (binding_id,),
        )
        return rows[0]["period"] if rows else None

    def processed_watch_keys_for_binding(self, binding_id: str) -> set[str]:
        """Return the set of file keys this binding has already attempted via
        the watch trigger path. Used by the watcher to skip files it's already
        seen — `triggered_by` is set to `'watch:<key>'` on the original call.

        Both successful and failed attempts count as 'processed' — re-triggering
        a failed load is an explicit admin operation, not the watcher's job.
        """
        rows = db.query_platform(
            """
            SELECT triggered_by
              FROM load_history
             WHERE binding_id = %s
               AND triggered_by LIKE 'watch:%%'
            """,
            (binding_id,),
        )
        keys: set[str] = set()
        for row in rows:
            tb = row["triggered_by"]
            if tb.startswith("watch:"):
                keys.add(tb[len("watch:"):])
        return keys
