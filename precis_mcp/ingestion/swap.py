# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Swap stage — promote `staging.<x>` into `live.<x>` atomically.

Runs after `validate.validate_staging_shape` confirms the structural
shape is intact. The dispatch is by `kind`:

- **kind='period'** — `ALTER TABLE live.<x> REPLACE PARTITION '<period>'
  FROM staging.<x>`. Per-period atomic; other periods on live are
  untouched. Idempotent on retry for the same `(target, period)`. A
  post-swap `ALTER TABLE staging.<x> DROP PARTITION '<period>'` clears
  the just-promoted slice from staging so the next load for that
  period writes into an empty partition.
- **kind='snapshot'** — `EXCHANGE TABLES live.<x> AND staging.<x>`.
  Atomic full-table swap; metadata-only, no row copy. After the swap,
  staging holds the previous snapshot — `ibis_executor.execute` calls
  `TRUNCATE TABLE IF EXISTS staging.<x>` before its INSERT on the next
  load, so no cleanup is needed here.

CH requires identical engine / PARTITION BY / ORDER BY between source
and target of REPLACE PARTITION. `live_ddl_runner.apply_all` enforces
that by applying the same body to both schemas.

This module renders + issues the SQL and bubbles up failures so the
orchestrator can mark the load `failed_swap`. Takes explicit `target`
+ `kind` rather than a `Binding` object — keeps it decoupled from the
registry's reshape and trivially testable.
"""

from __future__ import annotations

from typing import Any, Optional

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.swap")


class SwapError(Exception):
    """Raised on swap argument misuse — e.g. period missing for a
    'period' binding. CH-side failures propagate as the underlying
    clickhouse-connect exception; the orchestrator catches both and
    marks the load `failed_swap`.
    """


# ---------------------------------------------------------------------------
# SQL renderers — exposed for testing without needing a CH client.
# ---------------------------------------------------------------------------


def render_replace_partition_sql(target: str, period: str) -> str:
    """`ALTER TABLE live.<x> REPLACE PARTITION '<period>' FROM
    staging.<x>`.

    Period is quoted as a String literal — partition expressions on
    live tables return String (`PARTITION BY period` in the DDL), so
    the literal must match character-for-character.
    """
    staging = _staging_for(target)
    return f"ALTER TABLE {target} REPLACE PARTITION '{period}' FROM {staging}"


def render_exchange_sql(target: str) -> str:
    """`EXCHANGE TABLES live.<x> AND staging.<x>` — atomic identifier
    swap for snapshot bindings."""
    staging = _staging_for(target)
    return f"EXCHANGE TABLES {target} AND {staging}"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def swap(
    *,
    kind: str,
    period: Optional[str],
    target: str,
    load_id: str,
    ch_client: Any,
) -> None:
    """Dispatch to the kind-appropriate swap implementation.

    Args:
        kind: 'period' or 'snapshot'.
        period: Required for kind='period'; must be None for
                kind='snapshot'.
        target: Fully-qualified live table, e.g. 'live.fact_gl'. The
                staging counterpart is derived as 'staging.<table>'.
        load_id: For audit logging only — no semantic role at the swap
                 stage. Set by the orchestrator.
        ch_client: clickhouse-connect Client with `.command(sql)`.

    Raises:
        SwapError on argument misuse. CH-side errors propagate as the
        underlying clickhouse-connect exception.
    """
    if kind == "period":
        if period is None:
            raise SwapError(
                f"kind='period' requires a period; got None"
            )
        _swap_partition(period=period, target=target, load_id=load_id, ch_client=ch_client)
    elif kind == "snapshot":
        if period is not None:
            raise SwapError(
                f"kind='snapshot' must have period=None; got {period!r}"
            )
        _swap_snapshot(target=target, load_id=load_id, ch_client=ch_client)
    else:
        raise SwapError(
            f"kind={kind!r} must be 'period' or 'snapshot'"
        )


# ---------------------------------------------------------------------------
# Kind-specific implementations
# ---------------------------------------------------------------------------


def _swap_partition(
    *,
    period: str,
    target: str,
    load_id: str,
    ch_client: Any,
) -> None:
    """Issue REPLACE PARTITION, then clear the staging partition.

    The DROP PARTITION on staging is necessary because REPLACE PARTITION
    *copies* every row in staging's matching partition into live;
    without clearing, the next load for the same `(target, period)`
    would write into a partition that already contains the previous
    load's rows, promoting (old + new) on retry. The ingestion lock
    prevents concurrent loads for the same target — this DROP is
    race-safe by ordering.
    """
    staging = _staging_for(target)
    replace_sql = render_replace_partition_sql(target, period)
    _logger.info(
        "ingest.swap.start",
        load_id=load_id,
        kind="period",
        period=period,
        target=target,
        staging=staging,
    )
    ch_client.command(replace_sql)
    ch_client.command(f"ALTER TABLE {staging} DROP PARTITION '{period}'")
    _logger.info(
        "ingest.swap.done",
        load_id=load_id,
        kind="period",
        period=period,
        target=target,
    )


def _swap_snapshot(
    *,
    target: str,
    load_id: str,
    ch_client: Any,
) -> None:
    """Atomically swap live ↔ staging table identifiers.

    `EXCHANGE TABLES` is metadata-only — no row copy. After the swap,
    staging holds the previous snapshot. The next load's
    `ibis_executor.execute` does `TRUNCATE TABLE IF EXISTS staging.<x>`
    before its INSERT, so no cleanup is required here.
    """
    staging = _staging_for(target)
    exchange_sql = render_exchange_sql(target)
    _logger.info(
        "ingest.swap.start",
        load_id=load_id,
        kind="snapshot",
        period=None,
        target=target,
        staging=staging,
    )
    ch_client.command(exchange_sql)
    _logger.info(
        "ingest.swap.done",
        load_id=load_id,
        kind="snapshot",
        target=target,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _staging_for(target: str) -> str:
    """`live.<x>` → `staging.<x>`. Same table name in different schemas
    — `live_ddl_runner.apply_all` enforces this invariant."""
    if not target.startswith("live."):
        raise SwapError(
            f"target={target!r} must start with 'live.'"
        )
    table = target[len("live."):]
    return f"staging.{table}"