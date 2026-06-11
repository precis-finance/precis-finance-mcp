# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Execute an operator-authored extract query against the binding's
source via Ibis, and stream the result into `staging.<table>` in
ClickHouse.

The driver protocol (`warehouse_postgres.py` & friends) is gone.
Aggregation runs in the source — Ibis issues dialect-native SQL through
the source's own driver, so PG plans the GROUP BY, BigQuery plans its
own, etc. Only the result rows traverse the wire.

This module is deliberately stateless and takes explicit inputs rather
than a `Binding` object — keeps it decoupled from the registry's
reshape (in flight) and trivially testable with a stubbed Ibis backend
plus a CH fixture.

Pipeline position
-----------------
        ibis_executor       <-- this module
            ↓
        staging.<x>
            ↓                <-- shape validation
            ↓                <-- (future) user-supplied check SQL
            ↓
           swap              <-- REPLACE PARTITION / EXCHANGE TABLES
            ↓
         live.<x>

Idempotency
-----------
The same `(binding, period)` re-runs must overwrite, not append:

- `kind: period` — `ALTER TABLE staging.<x> DROP PARTITION '<period>'`
  before INSERT. CH treats DROP PARTITION on a non-existent partition
  as a no-op (first load is fine).
- `kind: snapshot` — `TRUNCATE TABLE IF EXISTS staging.<x>` before
  INSERT.

Parameter binding
-----------------
Operator queries reference `:period` as a placeholder. The runner
validates the orchestrator-supplied period against a strict regex
(calendar months, 13th periods, adjustment periods) before
substituting the value as a quoted SQL literal. The value space is
constrained — no injection surface.

Snapshot bindings pass `period=None`; the query is sent verbatim.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.ibis_executor")


# Accepts: calendar months ('2026-05'), 13th periods ('2026-13'), and
# adjustment periods ('2026-05-ADJ'). Validated at the binding layer
# AND here — defence in depth.
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2]|13)(?:-ADJ)?$")


class IbisExecutorError(Exception):
    """Raised on any failure inside the executor.

    The orchestrator catches and translates to `failed_extract` on the
    load.
    """


@dataclass
class ExecutorResult:
    rows_landed: int
    duration_ms: int


def execute(
    *,
    binding_id: str,
    source_id: str,
    kind: str,
    period: Optional[str],
    target: str,
    extract_query: str,
    load_id: str,
    ibis_backend: Any,
    ch_client: Any,
    template_vars: Optional[dict[str, str]] = None,
) -> ExecutorResult:
    """Run `extract_query` on `ibis_backend` and write rows into the
    staging twin of `target` via `ch_client`.

    Args:
        binding_id: For logging / error context. No semantic role.
        source_id: For logging / error context. No semantic role.
        kind: 'period' or 'snapshot'. Drives staging-clear behaviour
              (DROP PARTITION vs TRUNCATE) and period validation.
        period: Accounting period string. Required for kind='period',
                must be None for kind='snapshot'.
        target: Fully-qualified live table name, e.g. 'live.fact_gl'.
                Staging table is derived by swapping the 'live.' prefix
                for 'staging.'.
        extract_query: Operator SQL with optional ':period' placeholder.
        load_id: Audit tag written to the synthetic `_load_id` column on
                 every inserted row. `_ingested_at` is set by CH's
                 DEFAULT now().
        ibis_backend: Ibis connection to the source (e.g. from
                      `IbisRegistry.get_ibis_backend(source_id)`).
        ch_client: clickhouse-connect Client with `.command()` and
                   `.insert_df()`.

    Returns:
        ExecutorResult with rows_landed and duration_ms.

    Raises:
        IbisExecutorError on any failure (period validation, source
        query failure, CH write failure).
    """
    start = time.monotonic()

    _validate_period_args(kind, period)
    staging_table = _staging_table(target)

    # Idempotency: clear the slice we're about to write so retries of
    # the same (binding, period) replace in place.
    if kind == "period":
        ch_client.command(
            f"ALTER TABLE {staging_table} DROP PARTITION '{period}'"
        )
    else:
        ch_client.command(f"TRUNCATE TABLE IF EXISTS {staging_table}")

    source_query = _render_source_query(extract_query, period, template_vars)

    _logger.info(
        "ingestion.ibis_executor.source_query.start",
        binding_id=binding_id,
        source_id=source_id,
        period=period,
        target=target,
    )

    try:
        df = ibis_backend.sql(source_query).execute()
    except Exception as exc:
        raise IbisExecutorError(
            f"Source query execution failed for binding {binding_id!r} "
            f"period={period!r} on source={source_id!r}: {exc}"
        ) from exc

    rows_landed = len(df)

    if rows_landed > 0:
        # Inject _load_id; _ingested_at is filled by CH's DEFAULT now().
        df = df.assign(_load_id=load_id)
        try:
            ch_client.insert_df(staging_table, df)
        except Exception as exc:
            raise IbisExecutorError(
                f"Staging insert failed for binding {binding_id!r} "
                f"period={period!r} into {staging_table}: {exc}"
            ) from exc
    else:
        # Empty result is not an executor error — the orchestrator refuses
        # to swap a zero-row staging partition over live data; the
        # executor's job is just to write what the source returned.
        _logger.warning(
            "ingestion.ibis_executor.empty_result",
            binding_id=binding_id,
            period=period,
        )

    duration_ms = int((time.monotonic() - start) * 1000)

    _logger.info(
        "ingestion.ibis_executor.done",
        binding_id=binding_id,
        period=period,
        rows_landed=rows_landed,
        duration_ms=duration_ms,
    )

    return ExecutorResult(rows_landed=rows_landed, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_period_args(kind: str, period: Optional[str]) -> None:
    """Enforce the period × kind contract.

    kind='period' requires a non-null, regex-conformant period. kind='snapshot'
    requires period is None. Anything else is an executor-misuse error and
    raises rather than silently passing through.
    """
    if kind == "period":
        if period is None:
            raise IbisExecutorError(
                "kind='period' requires a period; got None"
            )
        if not _PERIOD_RE.match(period):
            raise IbisExecutorError(
                f"Period {period!r} does not match expected format "
                f"(YYYY-MM, YYYY-13, or YYYY-MM-ADJ)"
            )
    elif kind == "snapshot":
        if period is not None:
            raise IbisExecutorError(
                f"kind='snapshot' must have period=None; got {period!r}"
            )
    else:
        raise IbisExecutorError(
            f"kind={kind!r} must be 'period' or 'snapshot'"
        )


def _staging_table(target: str) -> str:
    """`live.<x>` → `staging.<x>`. Same table name in different schemas
    — `live_ddl_runner.apply_all` enforces this invariant."""
    if not target.startswith("live."):
        raise IbisExecutorError(
            f"target={target!r} must start with 'live.'"
        )
    table = target[len("live."):]
    return f"staging.{table}"


def _render_source_query(
    query: str,
    period: Optional[str],
    template_vars: Optional[dict[str, str]] = None,
) -> str:
    """Apply two substitutions to the operator's extract query:

    1. `:period` → quoted period literal (regex-validated upstream so
       this is injection-safe). Skipped when `period is None`
       (snapshot bindings).
    2. `${var_name}` → value from `template_vars`. Used for
       file-drop bindings to substitute the resolved source path
       (`${source_path}`) and similar deployment-time values.

    The substitutions are independent; either can be present alone.
    """
    rendered = query
    if period is not None:
        rendered = rendered.replace(":period", f"'{period}'")
    if template_vars:
        for name, value in template_vars.items():
            rendered = rendered.replace(f"${{{name}}}", value)
    return rendered