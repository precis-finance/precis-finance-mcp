# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Post-load validation: compare the staging table's structural shape
against the declared live shape.

Runs after `ibis_executor.execute` writes into `staging.<x>` and before
the swap promotes `staging.<x>` into `live.<x>`. The source of truth
for the expected shape is ClickHouse itself — both tables are created
by `live_ddl_runner.apply_all` from the same `instance/live/<x>.sql`
body. The validator queries `system.columns` for both and reports any
drift.

This is a guard rail, not an active validator — the two tables are
identical by construction. It catches:
  - Deployment skew (live created before a schema change, staging
    after, or vice versa).
  - Manual `ALTER TABLE` on one but not the other.
  - Mismatched re-applies of `instance/live/<x>.sql` (operator edited
    the file but the runner only re-applied against one schema).

This module is structural diff only. Operator-declared check SQL —
assertions over the staging rows like 'no negative amounts on revenue
accounts' — runs in the separate checks stage (`checks.py`), after this
diff and before the swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.validate")


class ValidationError(Exception):
    """Raised when validation can't run — distinct from validation
    *failure* (shapes differ), which is a normal `ValidationResult`
    return value. ValidationError means the call should not have been
    made at all (table missing → run `live_ddl_runner` first).
    """


@dataclass
class ValidationResult:
    target: str  # 'live.fact_gl'
    staging_table: str  # 'staging.fact_gl'
    passed: bool
    missing_in_staging: tuple[str, ...] = ()
    extra_in_staging: tuple[str, ...] = ()
    type_mismatches: tuple[tuple[str, str, str], ...] = ()
    # (col, live_type, staging_type) where types differ after whitespace
    # normalisation.

    def summary(self) -> str:
        """Human-readable single-block summary. Used in operator logs
        and error messages on swap-gate fail."""
        if self.passed:
            return f"{self.target}: shape OK vs {self.staging_table}"
        parts = [f"{self.target}: shape drift vs {self.staging_table}"]
        if self.missing_in_staging:
            parts.append(
                f"  missing in staging: {list(self.missing_in_staging)}"
            )
        if self.extra_in_staging:
            parts.append(
                f"  extra in staging:   {list(self.extra_in_staging)}"
            )
        if self.type_mismatches:
            parts.append("  type mismatches (col, live, staging):")
            for col, lt, st in self.type_mismatches:
                parts.append(f"    {col}: {lt!r} → {st!r}")
        return "\n".join(parts)


def validate_staging_shape(
    *,
    target: str,
    ch_client: Any,
) -> ValidationResult:
    """Compare the column shape of `target` (live.<x>) against its
    staging twin (staging.<x>). Returns a `ValidationResult` describing
    the diff.

    `passed=True` iff names and types match exactly (whitespace in
    types is normalised — `Decimal(18, 2)` == `Decimal(18,2)`).

    Args:
        target: Fully-qualified live table, e.g. 'live.fact_gl'.
        ch_client: clickhouse-connect Client with `.query()`.

    Raises:
        ValidationError if `target` doesn't start with 'live.' or if
        either live or staging table is missing.
    """
    if not target.startswith("live."):
        raise ValidationError(
            f"target={target!r} must start with 'live.'"
        )
    table = target[len("live."):]
    staging_table = f"staging.{table}"

    live_cols = _read_column_shape(ch_client, "live", table)
    staging_cols = _read_column_shape(ch_client, "staging", table)

    if live_cols is None:
        raise ValidationError(
            f"Live table {target!r} does not exist. Run "
            f"`live_ddl_runner.apply_all` first."
        )
    if staging_cols is None:
        raise ValidationError(
            f"Staging table {staging_table!r} does not exist. Run "
            f"`live_ddl_runner.apply_all` first."
        )

    live_names = set(live_cols)
    staging_names = set(staging_cols)
    missing = tuple(sorted(live_names - staging_names))
    extra = tuple(sorted(staging_names - live_names))

    type_mismatches: list[tuple[str, str, str]] = []
    for col in sorted(live_names & staging_names):
        if _normalise_type(live_cols[col]) != _normalise_type(
            staging_cols[col]
        ):
            type_mismatches.append((col, live_cols[col], staging_cols[col]))

    passed = not missing and not extra and not type_mismatches

    result = ValidationResult(
        target=target,
        staging_table=staging_table,
        passed=passed,
        missing_in_staging=missing,
        extra_in_staging=extra,
        type_mismatches=tuple(type_mismatches),
    )

    if passed:
        _logger.info(
            "ingestion.validate.passed",
            target=target,
            columns=len(live_cols),
        )
    else:
        _logger.warning(
            "ingestion.validate.failed",
            target=target,
            missing=list(missing),
            extra=list(extra),
            mismatches=type_mismatches,
        )

    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_column_shape(
    ch_client: Any, database: str, table: str
) -> Optional[dict[str, str]]:
    """Return `{col_name: type}` for the named table, ordered by
    declared position, or None if the table doesn't exist. Uses
    `system.columns` so types come back as CH stores them."""
    rows = ch_client.query(
        f"SELECT name, type FROM system.columns "
        f"WHERE database = '{database}' AND table = '{table}' "
        f"ORDER BY position"
    ).result_rows
    if not rows:
        return None
    return {row[0]: row[1] for row in rows}


def _normalise_type(t: str) -> str:
    """Strip whitespace from a CH type string. CH's
    `system.columns.type` renders `Decimal(18, 2)` with a space after
    the comma; an instance/live/<x>.sql body may render
    `Decimal(18,2)`. Same type — don't flag the whitespace as drift."""
    return "".join(t.split())