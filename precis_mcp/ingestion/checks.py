# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Data-quality checks — the validate-stage control gate.

Runs the binding's operator-declared `checks:` against `staging.<x>` after
the structural shape check passes and before the swap. Every non-reconcile
check reduces to a **failing-row count** (the dbt convention: zero rows =
pass); `reconcile` diffs staged-side aggregates against an authoritative
source total captured during extract.

A check trips when its failing count satisfies its `threshold` (default
`> 0` — any failing row). What a trip does is its `severity`:

  error    → blocks the swap (verdict `failed_checks`)
  warning  → loads, but the run is `succeeded_with_warnings`
  info     → recorded only; never affects the verdict

The per-check results + verdict are written to `load_history.control_total_result`
and surfaced by the `list_load_history` / `get_load_status` MCP tools.

Pipeline position: orchestrator.run_binding → (extract, capturing reconcile
source totals) → validate shape → **run_checks** → swap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from precis_mcp.ingestion.registry import Binding, Check
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.checks")

# Verdict values. PASSED / WARNINGS keep load_history.status = 'success'
# (warnings landed data); FAILED maps the load to status 'failed_checks'.
VERDICT_PASSED = "passed"
VERDICT_WARNINGS = "succeeded_with_warnings"
VERDICT_FAILED = "failed_checks"

_THRESHOLD_RE = re.compile(r"^\s*(>=|<=|!=|=|>|<)\s*(\d+)\s*$")
_OPS = {
    ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


class CheckError(Exception):
    """A check could not be compiled (programming/contract error, not a
    data failure). Data failures are normal CheckResult values."""


@dataclass
class CheckResult:
    name: str
    severity: str
    type: Optional[str]
    passed: bool
    failing: Optional[int] = None      # failing rows / groups
    threshold: Optional[str] = None
    error: Optional[str] = None        # set if the check could not run
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name, "severity": self.severity,
            "type": self.type, "passed": self.passed,
        }
        if self.failing is not None:
            d["failing"] = self.failing
        if self.threshold is not None:
            d["threshold"] = self.threshold
        if self.error is not None:
            d["error"] = self.error
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class CheckRunResult:
    verdict: str
    results: list[CheckResult]

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_FAILED

    def to_jsonb(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "checks": [r.to_dict() for r in self.results]}

    def summary(self) -> str:
        tripped = [r for r in self.results if not r.passed]
        if not tripped:
            return f"{len(self.results)} checks passed"
        parts = [f"{r.name} [{r.severity}]"
                 + (f" — {r.error}" if r.error else f" ({r.failing} failing)")
                 for r in tripped]
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def reconcile_checks(binding: Binding) -> list[Check]:
    """The binding's reconcile checks — the orchestrator captures their
    `source_query` totals during extract (when the source connection is open)."""
    return [c for c in binding.checks if c.type == "reconcile"]


def run_checks(
    binding: Binding,
    *,
    ch_client: Any,
    reconcile_source_totals: Optional[dict[str, list[dict]]] = None,
) -> CheckRunResult:
    """Run every check on the binding against `staging.<x>` and decide the verdict.

    `reconcile_source_totals` maps a reconcile check's name to the source-side
    grouped rows captured during extract (`list[dict]`, keys = group_by columns
    + measure names). Missing for a reconcile check → that check errors.
    """
    staging = binding.staging_target
    source_totals = reconcile_source_totals or {}
    results: list[CheckResult] = []

    for c in binding.checks:
        try:
            if c.type == "reconcile":
                res = _run_reconcile(c, ch_client, staging, source_totals.get(c.name))
            else:
                failing = _failing_count(c, ch_client, staging)
                res = CheckResult(
                    name=c.name, severity=c.severity, type=c.type,
                    passed=not _trips(failing, c.threshold),
                    failing=failing, threshold=c.threshold,
                )
        except CheckError:
            raise
        except Exception as exc:
            # A check that cannot run is not a control that passed. Record the
            # error; it counts as a trip (fail-closed for error severity).
            res = CheckResult(
                name=c.name, severity=c.severity, type=c.type,
                passed=False, error=str(exc)[:500],
            )
            _logger.warning("ingestion.checks.errored", check=c.name, error=str(exc))
        results.append(res)

    error_tripped = any(not r.passed and r.severity == "error" for r in results)
    warning_tripped = any(not r.passed and r.severity == "warning" for r in results)
    verdict = (
        VERDICT_FAILED if error_tripped
        else VERDICT_WARNINGS if warning_tripped
        else VERDICT_PASSED
    )
    return CheckRunResult(verdict=verdict, results=results)


# ---------------------------------------------------------------------------
# Failing-row count (curated types + raw sql)
# ---------------------------------------------------------------------------


def _failing_count(c: Check, ch_client: Any, staging: str) -> int:
    inner = _failing_rows_sql(c, staging)
    rows = ch_client.query(
        f"SELECT count() FROM ({inner}) AS _precis_check"
    ).result_rows
    return int(rows[0][0]) if rows else 0


def _failing_rows_sql(c: Check, staging: str) -> str:
    """The SELECT that yields the violating rows (or a 1-row marker for
    table-level metric checks). Wrapped in `count()` by the caller."""
    if c.sql is not None:
        live = "live." + staging[len("staging."):]
        return c.sql.replace("{staging}", staging).replace("{live}", live)

    w = c.where
    t = c.type

    def row_pred(pred: str) -> str:
        clause = f"({w}) AND ({pred})" if w else pred
        return f"SELECT 1 FROM {staging} WHERE {clause}"

    def grouped(group_cols: str, having: str) -> str:
        where = f"WHERE {w} " if w else ""
        return f"SELECT 1 FROM {staging} {where}GROUP BY {group_cols} HAVING {having}"

    if t == "not_null":
        return row_pred(f"{c.column} IS NULL")
    if t == "accepted_values":
        vals = ", ".join(_lit(v) for v in (c.values or []))
        return row_pred(f"{c.column} NOT IN ({vals})")
    if t == "accepted_range":
        preds = []
        if c.min is not None:
            preds.append(f"{c.column} < {c.min}")
        if c.max is not None:
            preds.append(f"{c.column} > {c.max}")
        return row_pred(" OR ".join(preds))
    if t == "referential":
        rs, rt, rc = (c.references or "").split(".")
        return row_pred(f"{c.column} NOT IN (SELECT {rc} FROM {rs}.{rt})")
    if t == "unique":
        return grouped(", ".join(c.columns or []), "count() > 1")
    if t == "expression":
        if c.group_by:
            return grouped(", ".join(c.group_by), f"NOT ({c.expression})")
        return row_pred(f"NOT ({c.expression})")
    if t == "row_count":
        metric = f"(SELECT count() FROM {staging}{_where_suffix(w)})"
        return f"SELECT 1 WHERE {_bounds_violation(metric, c.min, c.max)}"
    if t == "distinct_count":
        metric = f"(SELECT count(DISTINCT {c.column}) FROM {staging}{_where_suffix(w)})"
        return f"SELECT 1 WHERE {_bounds_violation(metric, c.min, c.max)}"
    raise CheckError(f"check {c.name!r}: unsupported type {t!r}")


# ---------------------------------------------------------------------------
# Reconcile — staged-vs-source diff at the group_by grain
# ---------------------------------------------------------------------------


def _run_reconcile(
    c: Check, ch_client: Any, staging: str, source_rows: Optional[list[dict]]
) -> CheckResult:
    measures = c.measures or {}
    group_by = c.group_by or []
    if source_rows is None:
        raise CheckError(
            f"reconcile {c.name!r}: source totals were not captured during extract"
        )

    # Staged side: project group_by + each measure (in declared order) and
    # read positionally — no dependency on the driver returning column names.
    measure_names = list(measures)
    select_cols = group_by + [f"{measures[m].expr} AS {m}" for m in measure_names]
    staged_sql = (
        f"SELECT {', '.join(select_cols)} FROM {staging}"
        f"{_where_suffix(c.where)} GROUP BY {', '.join(group_by)}"
    )
    staged: dict[tuple, dict[str, float]] = {}
    for row in ch_client.query(staged_sql).result_rows:
        key = tuple(row[: len(group_by)])
        staged[key] = {
            m: _num(row[len(group_by) + i]) for i, m in enumerate(measure_names)
        }

    source: dict[tuple, dict[str, float]] = {}
    for r in source_rows:
        key = tuple(r[g] for g in group_by)
        source[key] = {m: _num(r.get(m)) for m in measure_names}

    failing_groups = 0
    worst: dict[str, dict[str, float]] = {}  # measure -> {gap, staged, source}
    for key in set(staged) | set(source):
        s, src = staged.get(key), source.get(key)
        if s is None or src is None:
            failing_groups += 1                      # grain mismatch
            continue
        group_failed = False
        for m in measure_names:
            gap = abs(s[m] - src[m])
            if not _within_tolerance(gap, src[m], measures[m].tolerance):
                group_failed = True
                if gap > worst.get(m, {}).get("gap", -1.0):
                    worst[m] = {"gap": gap, "staged": s[m], "source": src[m]}
        if group_failed:
            failing_groups += 1

    detail: dict[str, Any] = {
        "groups": {"staged": len(staged), "source": len(source)},
        "measures": {
            m: {
                "tolerance": measures[m].tolerance,
                **({"worst_gap": worst[m]} if m in worst else {}),
            }
            for m in measure_names
        },
    }
    return CheckResult(
        name=c.name, severity=c.severity, type="reconcile",
        passed=not _trips(failing_groups, c.threshold),
        failing=failing_groups, threshold=c.threshold, detail=detail,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trips(failing: int, threshold: Optional[str]) -> bool:
    """A check trips when `failing <op> N` holds. Default threshold `> 0`
    (any failing row trips)."""
    if threshold is None:
        return failing > 0
    m = _THRESHOLD_RE.match(threshold)
    if not m:  # validated at the model layer; defensive
        return failing > 0
    op, n = m.group(1), int(m.group(2))
    return _OPS[op](failing, n)


def _within_tolerance(gap: float, source_val: float, tolerance: dict) -> bool:
    if "abs" in tolerance:
        return gap <= tolerance["abs"]
    if "pct" in tolerance:
        denom = abs(source_val)
        return gap == 0.0 if denom == 0 else (gap / denom) <= tolerance["pct"]
    return gap == 0.0


def _bounds_violation(metric: str, lo: Optional[float], hi: Optional[float]) -> str:
    preds = []
    if lo is not None:
        preds.append(f"{metric} < {lo}")
    if hi is not None:
        preds.append(f"{metric} > {hi}")
    return " OR ".join(preds)


def _where_suffix(where: Optional[str]) -> str:
    return f" WHERE {where}" if where else ""


def _lit(v: Any) -> str:
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _num(v: Any) -> float:
    return float(v) if v is not None else 0.0
