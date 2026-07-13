# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis_mcp.engine.catalogue import BaseMetric, Catalogue, DerivedMetric, MetricPredicate
from precis_mcp.engine.query_extensions import scenario_sql_scope
from precis_mcp.engine.resolver import GrainSpec, shift_period, shift_year
from precis_mcp.engine.types import ROLLED_UP, DimensionKey, RawResults

# Column names in dimension filters must be valid SQL identifiers.
_SAFE_COLUMN_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# The fact (domain source view) is always aliased so derived breakdown axes can
# join their leaf dimension table without column-name collisions. Every fact
# column reference is qualified with this alias; joined dim columns use d0/d1/…
_FACT = "t"

# Time-hierarchy dimensions are resolved as columns on the fact view, not via a
# join. Their rollup lives in the semantic layer (period parents are
# denormalised); a join would also entangle the non-additive closing-metric path.
# week/day are finer grains denormalised on facts that declare them (their
# prior-period join to the calendar is separate — see the calendar seq path).
_TIME_HIERARCHY_DIMS = {"period", "quarter", "fiscal_year", "week", "day"}


def _qualify(column: str) -> str:
    """Prefix a fact-view column with the fact alias. Literal source values
    (e.g. ``"1"`` for a row count) are not identifiers and pass through."""
    if _SAFE_COLUMN_RE.match(column):
        return f"{_FACT}.{column}"
    return column

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Minimal DataQuery / ExecutionPlan dataclasses
# (The resolver will produce these; defined here for independence.)
# ---------------------------------------------------------------------------

@dataclass
class DataQuery:
    """Represents a single ClickHouse query within an execution plan."""
    scenario_key: str          # e.g. 'actuals'
    scenario_id: str           # e.g. 'ACTUALS' — value stored in CH column
    period_start: str          # e.g. '2025-01'
    period_end: str            # e.g. '2025-12'
    metric_keys: list[str]     # base metric keys to fetch
    domain: str = "pnl"       # catalogue domain
    modifiers: dict[str, str] = field(default_factory=dict)  # e.g. {"uncommitted": ""}
    time_offset: int = 0       # shifted scenario offset in months (e.g. -12 for prior_year)
    period_column: str = "period"  # fact-view column the range filter targets (grain-dependent)
    native_column: str = "period"  # fact's native time-grain column — avg/closing rollup axis
    calendar_table: str = ""   # irregular-grain PP: seq calendar to join (e.g. semantic.dim_week)
    calendar_key: str = ""     # its key column (week/day); empty = arithmetic window shift
    query_extension: dict[str, object] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    """Full execution plan produced by the resolver."""
    data_queries: list[DataQuery]
    dimensions: list[str]      # e.g. [], ['cost_centre'], ['period'], ['quarter']
    grains: GrainSpec = field(default_factory=GrainSpec)


# ---------------------------------------------------------------------------
# SQL expression builders
# ---------------------------------------------------------------------------

def _sql_literal(value: object) -> str:
    """Render a trusted catalogue literal into a ClickHouse SQL fragment."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def compile_predicates_to_sql(where: list[MetricPredicate]) -> str:
    """Compile a metric's ``where`` predicates into a ClickHouse boolean SQL
    fragment for use inside ``CASE WHEN {filt} THEN ...``. Empty -> ``1=1``.

    Values are trusted catalogue literals; columns are validated at load.
    """
    if not where:
        return "1=1"

    parts: list[str] = []
    for pred in where:
        if not _SAFE_COLUMN_RE.match(pred.column):
            raise ValueError(f"Invalid predicate column name: {pred.column!r}")
        col = f"{_FACT}.{pred.column}"
        if pred.op == "eq":
            parts.append(f"{col} = {_sql_literal(pred.value)}")
        elif pred.op == "neq":
            parts.append(f"{col} != {_sql_literal(pred.value)}")
        elif pred.op == "gt":
            parts.append(f"{col} > {_sql_literal(pred.value)}")
        elif pred.op == "gte":
            parts.append(f"{col} >= {_sql_literal(pred.value)}")
        elif pred.op == "lt":
            parts.append(f"{col} < {_sql_literal(pred.value)}")
        elif pred.op == "lte":
            parts.append(f"{col} <= {_sql_literal(pred.value)}")
        elif pred.op == "in":
            vals = ", ".join(_sql_literal(v) for v in pred.values)
            parts.append(f"{col} IN ({vals})")
        elif pred.op == "not_in":
            vals = ", ".join(_sql_literal(v) for v in pred.values)
            parts.append(f"{col} NOT IN ({vals})")
        elif pred.op == "is_null":
            parts.append(f"{col} IS NULL")
        elif pred.op == "is_not_null":
            parts.append(f"{col} IS NOT NULL")

    return " AND ".join(parts)


def build_metric_expression(metric: BaseMetric) -> str:
    """Build the SQL CASE WHEN expression for a single base metric.

    Returns the expression WITHOUT an alias — caller adds 'AS {key}'.

    sign values:
      raw     -> SUM(CASE WHEN {filter} THEN {col} ELSE 0 END)
      abs     -> SUM(CASE WHEN {filter} THEN ABS({col}) ELSE 0 END)
      negate  -> SUM(CASE WHEN {filter} THEN -{col} ELSE 0 END)
    """
    col = _qualify(metric.source_column)
    filt = compile_predicates_to_sql(metric.where)

    if metric.aggregation == "count_distinct":
        # `source_column` is the distinct key (e.g. opportunity_id, employee_id).
        return f"COUNT(DISTINCT CASE WHEN {filt} THEN {col} END)"

    if metric.aggregation == "count":
        # Counts rows matching the filter. `source_column` is ignored — if
        # the caller needs "count non-null values of column X", that's
        # count_distinct over an appropriate key, or a derived metric.
        # `sign` is not meaningful for count and is also ignored.
        return f"COUNT(CASE WHEN {filt} THEN 1 END)"

    if metric.sign == "abs":
        value_expr = f"ABS({col})"
    elif metric.sign == "negate":
        value_expr = f"-{col}"
    else:  # raw
        value_expr = col

    return f"SUM(CASE WHEN {filt} THEN {value_expr} ELSE 0 END)"


def build_avg_metric_expression(metric: BaseMetric, native_column: str = "period") -> str:
    """Build avg-rollup expression: SUM(CASE WHEN ...) / COUNT(DISTINCT <native>).

    The denominator counts distinct periods at the fact's native grain, so the
    average is per native period within each breakdown group — correct at any
    query/breakdown grain, not just month. NULLIF guards a zero period count.
    """
    col = _qualify(metric.source_column)
    filt = compile_predicates_to_sql(metric.where)
    native = _qualify(native_column)

    if metric.sign == "abs":
        value_expr = f"ABS({col})"
    elif metric.sign == "negate":
        value_expr = f"-{col}"
    else:
        value_expr = col

    return (
        f"SUM(CASE WHEN {filt} THEN {value_expr} ELSE 0 END)"
        f" / NULLIF(COUNT(DISTINCT {native}), 0)"
    )


# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------

def _base_metrics_for_query(
    data_query: DataQuery,
    catalogue: Catalogue,
) -> list[BaseMetric]:
    """Return the BaseMetric objects requested in data_query, in order."""
    metrics: list[BaseMetric] = []
    for key in data_query.metric_keys:
        m = catalogue.metrics.get(key)
        if m is None:
            raise KeyError(f"Unknown metric key: {key!r}")
        if not isinstance(m, BaseMetric):
            raise TypeError(
                f"Metric {key!r} is a DerivedMetric — only BaseMetrics can be fetched from SQL"
            )
        metrics.append(m)
    return metrics


@dataclass
class _Join:
    """A join to a dimension table (or subquery) for a breakdown axis.

    Derived breakdowns use a ``LEFT JOIN`` to the leaf dimension table. The ragged
    breakdown path uses an ``INNER JOIN`` to a node→leaf subquery and matches the
    stringified leaf key (``cast_fact``, since the rollup stores ``toString``).
    """
    alias: str          # d0, d1, … (or 'b' for the ragged node join)
    table: str          # semantic.dim_cost_centre, or a "(subquery)" string
    fact_col: str       # the leaf's bound column on the fact view
    dim_key_col: str    # the joined table's key column
    kind: str = "LEFT"       # LEFT | INNER
    cast_fact: bool = False  # wrap the fact column in toString() in the ON

    def sql(self) -> str:
        left = (
            f"toString({_FACT}.{self.fact_col})" if self.cast_fact
            else f"{_FACT}.{self.fact_col}"
        )
        return (
            f"{self.kind} JOIN {self.table} {self.alias} "
            f"ON {left} = {self.alias}.{self.dim_key_col}"
        )


def _resolve_breakdowns(
    dimensions: list[str],
    catalogue: Catalogue,
    domain_cat,
) -> tuple[dict[str, str], list[_Join]]:
    """Resolve each breakdown axis to a qualified SQL expression plus any joins.

    - Bound axes (leaf dimensions, period, or an explicit derived binding) read a
      fact-view column: ``t.<column>``.
    - Time-hierarchy parents (quarter/fiscal_year) stay denormalised on the fact
      view: ``t.<name>``.
    - Other derived/parent axes join their leaf dimension table and group by the
      derived value column: ``d0.<column>``. One join per leaf, deduplicated.

    Returns ``(name_to_expr, joins)``. Raises ``KeyError`` for a derived axis
    whose leaf is not bound to this domain (so it cannot be joined).
    """
    bound = {cd.key: cd.source for cd in domain_cat.dimensions if cd.source}
    name_to_expr: dict[str, str] = {}
    joins: list[_Join] = []
    join_by_key: dict[tuple[str, str], _Join] = {}

    for name in dimensions:
        if name in bound:
            name_to_expr[name] = f"{_FACT}.{bound[name]}"
            continue
        if name in _TIME_HIERARCHY_DIMS:
            name_to_expr[name] = f"{_FACT}.{name}"
            continue

        dim = catalogue.dimensions.get(name)
        resolution = None
        if dim is not None:
            for leaf_key, res in dim._transitive.items():
                if leaf_key in bound:
                    resolution = res
                    break
        if resolution is None:
            raise KeyError(
                f"Dimension {name!r} is not groupable on domain "
                f"{domain_cat.domain!r}: it resolves to no leaf dimension bound "
                "to this domain."
            )

        fact_col = bound[resolution.leaf_dimension]
        join_key = (resolution.source_table, fact_col)
        join = join_by_key.get(join_key)
        if join is None:
            join = _Join(
                alias=f"d{len(joins)}",
                table=resolution.source_table,
                fact_col=fact_col,
                dim_key_col=resolution.leaf_key_column,
            )
            joins.append(join)
            join_by_key[join_key] = join
        name_to_expr[name] = f"{join.alias}.{resolution.filter_column}"

    return name_to_expr, joins


def _select_cols(
    metrics: list[BaseMetric],
    rollup_group: str | None,
    dimensions: list[str],
    name_to_expr: dict[str, str],
    native_column: str = "period",
) -> str:
    """Build SELECT column list.

    Dimension axes are selected as ``<expr> AS <catalogue name>`` so the result
    reader keys rows by the catalogue name regardless of how (or where) the
    underlying column lives.

    rollup_group controls which expression builder to use:
      'avg'     -> build_avg_metric_expression
      otherwise -> build_metric_expression
    """
    parts: list[str] = [f"{name_to_expr[dim]} AS {dim}" for dim in dimensions]

    for m in metrics:
        if rollup_group == "avg":
            expr = build_avg_metric_expression(m, native_column)
        else:
            expr = build_metric_expression(m)
        parts.append(f"{expr} AS {m.key}")

    return "\n    , ".join(parts)


def _where_clause(
    data_query: DataQuery,
    rollup_group: str | None,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    closing_only: bool = False,
    closing_time_dims: list[str] | None = None,
    source_view: str | None = None,
    versioned: bool = True,
) -> tuple[str, dict]:
    """Build WHERE clause and params dict.

    Applies scenario modifiers from ``data_query.modifiers``:
    - No modifiers (default): ``commit_id != '__uncommitted__'``
      (committed-only for plan scenarios; harmless for actuals).
    - ``uncommitted``: no commit_id filter (shows everything).
    - ``uncommitted_delta``: ``commit_id = '__uncommitted__'`` only.
    - ``commit={id}``: all commits up to and including the given ID.
    - ``commit_delta={id}``: single commit only.

    closing_time_dims: when closing_only=True and the query groups by
    time-hierarchy columns (quarter, fiscal_year), list those columns here.
    Instead of filtering to period_end globally, a subquery selects the
    last period within each time-dimension group.
    """
    conditions: list[str] = []
    params: dict = {
        "scenario_id": data_query.scenario_id,
        "period_start": data_query.period_start,
        "period_end": data_query.period_end,
    }

    modifiers = data_query.modifiers if versioned else {}
    delta_only = bool(
        {"uncommitted_delta", "commit_delta"}.intersection(modifiers)
    )
    extension_scope = scenario_sql_scope(data_query, delta_only)
    if extension_scope is not None:
        params.update(extension_scope.params)
        conditions.append(extension_scope.outer_condition)
        inner_scenario_condition = extension_scope.inner_condition
    else:
        conditions.append(f"{_FACT}.scenario = {{scenario_id:String}}")
        inner_scenario_condition = "scenario = {scenario_id:String}"

    # The additive range filter targets the grain's column (period for month,
    # quarter/fiscal_year for coarser grains). The closing paths below stay on
    # `period` — the resolver guards non-month grain away from closing metrics.
    period_col = _qualify(data_query.period_column)

    if data_query.calendar_table and _SAFE_COLUMN_RE.match(data_query.calendar_key):
        # Irregular-grain prior-period (week/day): filter to the predecessor of
        # each period in the (unshifted) window, resolved via the calendar's
        # dense seq (predecessor = seq - 1). Single fact query, calendar joined
        # as a subquery — no extra round-trip, no in-memory calendar.
        cal, key = data_query.calendar_table, data_query.calendar_key
        conditions.append(
            f"{period_col} IN (SELECT prev.{key} FROM {cal} cur "
            f"INNER JOIN {cal} prev ON prev.seq = cur.seq - 1 "
            f"WHERE cur.{key} >= {{period_start:String}} "
            f"AND cur.{key} <= {{period_end:String}})"
        )
    elif closing_only:
        # Closing = the value(s) at the last *native*-grain period within the
        # window, per time-breakdown group. The window filter is on the filter
        # grain (period_col); the argmax axis is the fact's native grain. For a
        # monthly domain (native == 'period') this is byte-identical to the
        # pre-grain behaviour.
        native = data_query.native_column
        if closing_time_dims and source_view:
            # Per time-breakdown-group: last native period in each group. The
            # outer reference is t-qualified; the self-contained subquery is not.
            conditions.append(
                f"{period_col} >= {{period_start:String}} "
                f"AND {period_col} <= {{period_end:String}}"
            )
            outer_cols = ", ".join(f"{_FACT}.{d}" for d in closing_time_dims)
            inner_cols = ", ".join(closing_time_dims)
            conditions.append(
                f"({outer_cols}, {_FACT}.{native}) IN ("
                f"SELECT {inner_cols}, max({native}) "
                f"FROM {source_view} "
                f"WHERE {inner_scenario_condition} "
                f"AND {data_query.period_column} >= {{period_start:String}} "
                f"AND {data_query.period_column} <= {{period_end:String}} "
                f"GROUP BY {inner_cols})"
            )
        elif native == "period":
            # Monthly axis, no time breakdown: the last month is the filter end.
            conditions.append(f"{_FACT}.period = {{period_end:String}}")
        elif source_view:
            # Finer native grain, no time breakdown: the single last native
            # period in the window (e.g. last week of a month-filtered query).
            conditions.append(
                f"{period_col} >= {{period_start:String}} "
                f"AND {period_col} <= {{period_end:String}}"
            )
            conditions.append(
                f"{_FACT}.{native} = ("
                f"SELECT max({native}) FROM {source_view} "
                f"WHERE {inner_scenario_condition} "
                f"AND {data_query.period_column} >= {{period_start:String}} "
                f"AND {data_query.period_column} <= {{period_end:String}})"
            )
        else:
            conditions.append(f"{_FACT}.{native} = {{period_end:String}}")
    else:
        conditions.append(
            f"{period_col} >= {{period_start:String}} "
            f"AND {period_col} <= {{period_end:String}}"
        )

    # rollup_group controls expression builder only — not a DB column, no WHERE filter

    if dimension_filters:
        # An empty value list is a resolved deny-all scope (e.g. a user filter
        # disjoint from their dimension scope) — emit the predicate so it
        # matches nothing. Skipping it would invert deny-all into allow-all.
        for col, values in sorted(dimension_filters.items()):
            if not _SAFE_COLUMN_RE.match(col):
                raise ValueError(f"Invalid dimension column name: {col!r}")
            param_key = f"dimf_{col}"
            params[param_key] = values
            conditions.append(
                f"toString({_FACT}.{col}) IN ({{{param_key}:Array(String)}})"
            )

    # ----- Commit-awareness modifiers -----
    # Only apply commit_id filters for versioned domains (those whose source
    # view includes a commit_id column — e.g. v_pnl, v_gl).  Actuals-only
    # domains (timesheets, payroll, utilisation) have no commit_id column.
    if versioned:
        if "uncommitted_delta" in modifiers:
            # Only uncommitted changes
            commit_condition = f"{_FACT}.commit_id = '__uncommitted__'"
        elif "uncommitted" in modifiers:
            # Include everything (committed + uncommitted) — no commit_id filter
            commit_condition = "1 = 1"
        elif "commit_delta" in modifiers:
            # Changes from a single commit only
            commit_id = modifiers["commit_delta"]
            params["mod_commit_id"] = commit_id
            commit_condition = f"{_FACT}.commit_id = {{mod_commit_id:String}}"
        elif "commit" in modifiers:
            # Time travel: state as of a specific commit (all commits up to and including)
            target_commit = modifiers["commit"]
            params["mod_target_commit"] = target_commit
            params["mod_scenario_id_commits"] = data_query.scenario_id
            commit_condition = (
                f"{_FACT}.commit_id IN ("
                "SELECT commit_id FROM planning.commits "
                "WHERE scenario_id = {mod_scenario_id_commits:String} "
                "AND created_at <= ("
                "SELECT created_at FROM planning.commits "
                "WHERE commit_id = {mod_target_commit:String} LIMIT 1"
                ")"
                ")"
            )
        else:
            # Default: committed-only (exclude uncommitted changes).
            # For actuals, commit_id = '__actuals__' so this is harmless.
            commit_condition = f"{_FACT}.commit_id != '__uncommitted__'"

        if extension_scope is not None and extension_scope.commit_passthrough_condition:
            conditions.append(
                f"({extension_scope.commit_passthrough_condition} OR "
                f"({commit_condition}))"
            )
        else:
            conditions.append(commit_condition)

    where = "\nAND ".join(conditions)
    return where, params


# Column carrying the ClickHouse GROUPING() bitmask. Present only when more than
# the detail grain is requested; the bit for each dimension is 1 when that
# dimension is rolled up in the row, letting the reader tag the row's grain.
GROUPING_COL = "_grouping"


def _grouping_sets(dimensions: list[str], grains: GrainSpec) -> list[list[str]]:
    """Dimension subsets to aggregate at, derived from the requested grains.

    The full dimension list for detail, right-to-left prefixes for subtotals,
    and the empty list for the grand total.
    """
    sets: list[list[str]] = []
    if grains.detail:
        sets.append(list(dimensions))
    if grains.subtotals:
        for level in range(len(dimensions) - 1, 0, -1):
            sets.append(dimensions[:level])
    if grains.grand_total:
        sets.append([])
    return sets


def _group_clause_from_sets(
    dimensions: list[str],
    sets: list[list[str]],
    name_to_expr: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Build the GROUP BY clause and whether a GROUPING() tag column is needed
    from an explicit list of grouping sets.

    No dimensions, or a single set equal to the full dimension list, yields a
    plain GROUP BY (or nothing) and no tag column — identical SQL to a
    single-grain query. Anything else yields GROUP BY GROUPING SETS and signals
    that a GROUPING() column must be selected to tag each row's grain. The
    grouping keys are emitted as their resolved SQL expressions.
    """
    name_to_expr = name_to_expr or {}
    def _expr(d: str) -> str:
        return name_to_expr.get(d, d)
    if not dimensions:
        return "", False
    if sets == [list(dimensions)]:
        return f"GROUP BY {', '.join(_expr(d) for d in dimensions)}", False
    rendered = ", ".join("(" + ", ".join(_expr(d) for d in s) + ")" for s in sets)
    return f"GROUP BY GROUPING SETS ({rendered})", True


# ---------------------------------------------------------------------------
# Public: generate_sql
# ---------------------------------------------------------------------------

def generate_sql(
    data_query: DataQuery,
    catalogue: Catalogue,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    grains: GrainSpec = GrainSpec(),
) -> list[tuple[str, dict]]:
    """Generate SQL query(ies) for a DataQuery.

    Args:
        data_query:        The data query from the execution plan.
        catalogue:         The loaded catalogue.
        dimensions:        List of dimension names for GROUP BY (e.g. ['period'], ['cost_centre']).
        dimension_filters: Resolved dimension filters {view_col: [leaf_ids]}, or None.
        grains:            Which aggregation grains to emit. Default is detail only.

    Returns:
        List of (sql_string, params_dict) tuples.
        Up to 3 tuples (one per rollup_method group that has metrics).
        When no dimensions, returns a single aggregate row.
    """
    domain = catalogue.domains.get(data_query.domain)
    if domain is None:
        raise KeyError(f"Unknown catalogue domain: {data_query.domain!r}")
    source_view = domain.source_view
    versioned = domain.versioned

    all_base = _base_metrics_for_query(data_query, catalogue)

    # Detect which requested dimensions are time-grain levels so closing metrics
    # can pick the last native period per group instead of the global end. For a
    # monthly domain this is period's parents (quarter, fiscal_year), unchanged;
    # for a finer-native domain it is every time grain in the breakdown, so each
    # week/day/month group gets its own last native period.
    closing_time_dims: list[str] = []
    if domain.native_grain_column == "period":
        period_dim = catalogue.dimensions.get("period")
        if period_dim:
            period_parent_keys = set(period_dim.parents.keys())
            closing_time_dims = [d for d in dimensions if d in period_parent_keys]
    else:
        closing_time_dims = [d for d in dimensions if d in _TIME_HIERARCHY_DIMS]

    name_to_expr, joins = _resolve_breakdowns(dimensions, catalogue, domain)

    return _generate_aggregate_sql(
        data_query, all_base, source_view, dimensions, dimension_filters,
        versioned=versioned,
        closing_time_dims=closing_time_dims,
        grains=grains,
        name_to_expr=name_to_expr,
        joins=joins,
    )


def generate_ragged_sql(
    data_query: DataQuery,
    catalogue: Catalogue,
    ragged_breakdown: dict,
    dimension_filters: dict[str, list[str]] | None,
) -> list[tuple[str, dict]]:
    """SQL for a breakdown *by* a ragged hierarchy (the divergent retrieve path).

    The breakdown axis is ``node_id`` from an INNER JOIN to the hierarchy's
    ``_rollup`` restricted to the anchor node and its immediate children (from
    ``_edges``). Grouping by ``node_id`` yields one row per node — the anchor row
    is the total (its own deduped leaf set), the child rows the breakdown. Detail
    grain only: the parent-node row is the total, so no GROUPING SETS is needed.
    The rollup method machinery (sum / avg / closing / count_distinct) applies
    unchanged, since each node group is an independent aggregation over its own
    leaves.
    """
    domain = catalogue.domains.get(data_query.domain)
    if domain is None:
        raise KeyError(f"Unknown catalogue domain: {data_query.domain!r}")

    key = ragged_breakdown["dimension"]
    leaf = ragged_breakdown["leaf_dimension"]
    leaf_dim = catalogue.dimensions.get(leaf)
    if leaf_dim is None or leaf_dim.source is None:
        raise KeyError(f"Ragged hierarchy {key!r} has no resolvable leaf dimension")
    leaf_key = leaf_dim.source.key_column

    bound = {cd.key: cd.source for cd in domain.dimensions if cd.source}
    if leaf not in bound:
        raise KeyError(
            f"Ragged hierarchy {key!r} leaf {leaf!r} is not bound to domain "
            f"{domain.domain!r}"
        )
    fact_leaf_col = bound[leaf]

    stem = f"dim_{leaf}_{key}"
    rollup_view = f"semantic.{stem}_rollup"
    edges_view = f"semantic.{stem}_edges"

    # B: node_id → leaf, restricted to the anchor node plus its immediate children.
    subquery = (
        f"    SELECT node_id, {leaf_key} FROM {rollup_view}\n"
        f"    WHERE node_id = {{ragged_anchor:String}}\n"
        f"       OR node_id IN (\n"
        f"           SELECT child_node_id FROM {edges_view}\n"
        f"           WHERE parent_node_id = {{ragged_anchor:String}})"
    )
    b_join = _Join(
        alias="b",
        table=f"(\n{subquery}\n)",
        fact_col=fact_leaf_col,
        dim_key_col=leaf_key,
        kind="INNER",
        cast_fact=True,
    )

    return _generate_aggregate_sql(
        data_query,
        _base_metrics_for_query(data_query, catalogue),
        domain.source_view,
        dimensions=[key],
        dimension_filters=dimension_filters,
        versioned=domain.versioned,
        closing_time_dims=None,
        grains=GrainSpec(),  # detail only — the anchor-node row carries the total
        name_to_expr={key: "b.node_id"},
        joins=[b_join],
        extra_params={"ragged_anchor": ragged_breakdown["anchor_node_id"]},
    )


def _from_clause(source_view: str, joins: list[_Join]) -> str:
    """The aliased FROM with any derived-axis leaf joins appended."""
    sql = f"FROM {source_view} {_FACT}"
    for join in joins:
        sql += f"\n{join.sql()}"
    return sql


def _full_grouping_expr(
    dimensions: list[str], time_dims: list[str], grouped_dims: set[str],
    name_to_expr: dict[str, str] | None = None,
) -> str:
    """SQL reproducing GROUPING(<all dims>) for the closing-totals query.

    Time dimensions (and any non-time dimension never used as a group key) are
    absent from that query's GROUP BY and always rolled up, so they contribute a
    constant bit; dimensions that are group keys use GROUPING(). Bit weights are
    most-significant-bit-first (first dimension = highest bit), matching how
    _row_to_dimension_key decodes the tag.
    """
    name_to_expr = name_to_expr or {}
    n = len(dimensions)
    terms: list[str] = []
    for i, d in enumerate(dimensions):
        weight = 1 << (n - 1 - i)
        if d in grouped_dims:
            terms.append(f"GROUPING({name_to_expr.get(d, d)}) * {weight}")
        else:
            terms.append(str(weight))
    return " + ".join(terms)


def _closing_totals_query(
    data_query: DataQuery,
    metrics: list[BaseMetric],
    source_view: str,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    versioned: bool,
    time_dims: list[str],
    time_rolled_sets: list[list[str]],
    name_to_expr: dict[str, str],
    joins: list[_Join],
) -> tuple[str, dict]:
    """Totals for closing metrics where a time dimension is rolled up.

    Closing is non-additive over time, so these grains take the value at the
    global period_end (closing_only, no per-group max-period subquery) and
    aggregate across the non-time dimensions. The time columns are not in this
    query's GROUP BY — they are emitted as rolled-up placeholders and the
    GROUPING() tag is rebuilt for the full dimension list.
    """
    non_time = [d for d in dimensions if d not in time_dims]
    # Project each time-rolled grain onto the non-time dimensions it keeps live,
    # de-duplicating (different time-rolled grains can share a projection).
    seen: set[tuple[str, ...]] = set()
    nt_sets: list[list[str]] = []
    for s in time_rolled_sets:
        proj = [d for d in s if d in non_time]
        key = tuple(proj)
        if key not in seen:
            seen.add(key)
            nt_sets.append(proj)

    grouped_dims = {d for s in nt_sets for d in s}

    select_parts: list[str] = [f"{name_to_expr[d]} AS {d}" for d in non_time]
    select_parts += [f"'' AS {t}" for t in time_dims]
    select_parts += [f"{build_metric_expression(m)} AS {m.key}" for m in metrics]
    select_parts.append(
        f"{_full_grouping_expr(dimensions, time_dims, grouped_dims, name_to_expr)} AS {GROUPING_COL}"
    )

    where, params = _where_clause(
        data_query,
        rollup_group="closing",
        dimensions=non_time,
        dimension_filters=dimension_filters,
        closing_only=True,
        versioned=versioned,
        source_view=source_view,
    )

    sql = (
        "SELECT\n    " + "\n    , ".join(select_parts) + "\n"
        + _from_clause(source_view, joins) + "\n"
        f"WHERE {where}"
    )
    # A lone grand-total set has no group keys — plain aggregate, no GROUPING SETS.
    if nt_sets != [[]]:
        rendered = ", ".join(
            "(" + ", ".join(name_to_expr[d] for d in s) + ")" for s in nt_sets
        )
        sql += f"\nGROUP BY GROUPING SETS ({rendered})"
    return sql, params


def _generate_aggregate_sql(
    data_query: DataQuery,
    metrics: list[BaseMetric],
    source_view: str,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    versioned: bool = True,
    closing_time_dims: list[str] | None = None,
    grains: GrainSpec = GrainSpec(),
    name_to_expr: dict[str, str] | None = None,
    joins: list[_Join] | None = None,
    extra_params: dict | None = None,
) -> list[tuple[str, dict]]:
    """One query per rollup_method group present in metrics.

    ``extra_params`` is merged into every query's parameter dict — used by the
    ragged breakdown path to bind the anchor node id referenced in its join
    subquery.

    Each query covers the requested grains via GROUP BY GROUPING SETS, with a
    GROUPING() tag column when more than the detail grain is asked for. The
    closing group falls back to detail only when a time-hierarchy dimension is
    present, because a rolled-up closing balance over time is non-additive and
    needs a dedicated query.
    """
    name_to_expr = name_to_expr or {}
    joins = joins or []

    # Group metrics by rollup_method
    groups: dict[str, list[BaseMetric]] = {"sum": [], "avg": [], "closing": []}
    for m in metrics:
        groups[m.rollup_method].append(m)

    results: list[tuple[str, dict]] = []

    def _build(rollup_group: str, where_extra: dict, sets: list[list[str]]) -> None:
        select_cols = _select_cols(
            groups[rollup_group], rollup_group=rollup_group, dimensions=dimensions,
            name_to_expr=name_to_expr, native_column=data_query.native_column,
        )
        where, params = _where_clause(
            data_query,
            rollup_group=rollup_group,
            dimensions=dimensions,
            dimension_filters=dimension_filters,
            versioned=versioned,
            **where_extra,
        )
        group_clause, tag = _group_clause_from_sets(dimensions, sets, name_to_expr)
        if tag:
            grouping_cols = ", ".join(name_to_expr[d] for d in dimensions)
            select_cols += f"\n    , GROUPING({grouping_cols}) AS {GROUPING_COL}"
        sql = (
            f"SELECT\n    {select_cols}\n"
            + _from_clause(source_view, joins) + "\n"
            f"WHERE {where}"
        )
        if group_clause:
            sql += f"\n{group_clause}"
        results.append((sql, params))

    requested_sets = _grouping_sets(dimensions, grains)

    if groups["sum"]:
        _build("sum", {}, requested_sets)

    if groups["avg"]:
        _build("avg", {}, requested_sets)

    if groups["closing"]:
        # When the *native* grain is itself a breakdown dimension, each row is
        # already a single native period, so the closing value is correct as-is.
        # closing_only applies when the native grain is aggregated away or when
        # grouping by a coarser time dim (quarter, month, week over a day native).
        native_col = data_query.native_column
        native_in_dims = native_col in dimensions
        closing_where = {
            "closing_only": not native_in_dims,
            "closing_time_dims": closing_time_dims or None,
            "source_view": source_view,
        }
        time_dims = list(closing_time_dims or []) + ([native_col] if native_in_dims else [])
        if not time_dims:
            _build("closing", closing_where, requested_sets)
        else:
            # Grains that keep every time dimension live roll up additively and
            # use the normal closing query. Grains that roll up a time dimension
            # are non-additive over time and need the global-period_end query.
            time_live = [s for s in requested_sets if all(t in s for t in time_dims)]
            time_rolled = [s for s in requested_sets if not all(t in s for t in time_dims)]
            if time_live:
                _build("closing", closing_where, time_live)
            if time_rolled:
                results.append(
                    _closing_totals_query(
                        data_query, groups["closing"], source_view, dimensions,
                        dimension_filters, versioned, time_dims, time_rolled,
                        name_to_expr=name_to_expr, joins=joins,
                    )
                )

    if extra_params:
        return [(sql, {**params, **extra_params}) for sql, params in results]
    return results


# ---------------------------------------------------------------------------
# Execution (thin ClickHouse wrapper)
# ---------------------------------------------------------------------------

def execute_queries(
    queries: list[tuple[str, dict]],
    ch_client,
) -> list[list[dict]]:
    """Execute SQL queries against ClickHouse.

    Uses ClickHouse native parameterised queries (``{name:Type}`` syntax)
    to avoid SQL injection.

    Returns a list of result row lists, one per query.
    Each row is a dict of column_name -> value.
    """
    result_sets: list[list[dict]] = []
    for sql, params in queries:
        result = ch_client.query(sql, parameters=params)
        rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
        result_sets.append(rows)

    return result_sets


# ---------------------------------------------------------------------------
# Row -> DimensionKey helpers
# ---------------------------------------------------------------------------

def _row_to_dimension_key(row: dict, dimensions: list[str]) -> DimensionKey:
    """Extract dimension values from a result row into a tuple.

    When a GROUPING() tag column is present (multi-grain GROUPING SETS queries),
    each dimension rolled up in this row is set to ROLLED_UP so subtotal and
    grand-total rows stay distinct from detail rows that share ClickHouse's
    default-filled values. Without the tag column every dimension is live.
    The bitmask is most-significant-bit-first: the first dimension argument
    occupies the highest bit.
    """
    mask = row.get(GROUPING_COL)
    if mask is None:
        return tuple(str(row[dim]) for dim in dimensions if dim in row)
    bits = int(mask)
    n = len(dimensions)
    return tuple(
        ROLLED_UP if (bits >> (n - 1 - i)) & 1 else str(row[dim])
        for i, dim in enumerate(dimensions)
    )


def _merge_row_into_results(
    scenario_results: dict[DimensionKey, dict[str, float | None]],
    row: dict,
    metric_keys: list[str],
    dimensions: list[str],
) -> None:
    """Merge a single result row into the scenario results dict."""
    dim_key = _row_to_dimension_key(row, dimensions)
    if dim_key not in scenario_results:
        scenario_results[dim_key] = {}
    for key in metric_keys:
        if key in row:
            val = row[key]
            scenario_results[dim_key][key] = float(val) if val is not None else None


# ---------------------------------------------------------------------------
# Time dimension shifting for shifted scenarios
# ---------------------------------------------------------------------------

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_FISCAL_YEAR_RE = re.compile(r"^(\d{4})$")


def _shift_time_value(value: str, dim_name: str, offset_months: int) -> str:
    """Shift a time dimension value by offset_months.

    Supports period (YYYY-MM), quarter (YYYY-QN), and fiscal_year (YYYY) by month
    arithmetic. For the irregular grains week (YYYY-Www) and day (YYYY-MM-DD) only
    prior-year remaps reach here (prior-period at those grains uses the calendar
    seq and sets time_offset=0, so no remap fires); a prior-year offset is a whole
    number of years, applied by shifting the year token.
    """
    if dim_name in ("week", "day"):
        return shift_year(value, offset_months // 12)

    if dim_name == "period":
        return shift_period(value, offset_months)

    if dim_name == "quarter":
        m = _QUARTER_RE.match(value)
        if m:
            year, q = int(m.group(1)), int(m.group(2))
            # Convert to start month, shift, convert back
            start_month = (q - 1) * 3 + 1
            shifted = shift_period(f"{year:04d}-{start_month:02d}", offset_months)
            new_year, new_month = int(shifted[:4]), int(shifted[5:7])
            new_q = (new_month - 1) // 3 + 1
            return f"{new_year:04d}-Q{new_q}"
        return value

    if dim_name == "fiscal_year":
        m = _FISCAL_YEAR_RE.match(value)
        if m:
            year = int(m.group(1))
            # Shift by full years (offset_months / 12)
            shifted = shift_period(f"{year:04d}-01", offset_months)
            return shifted[:4]
        return value

    return value


def _successor_remap(ch_client, calendar_table: str, calendar_key: str,
                     period_start: str, period_end: str) -> dict[str, str]:
    """Map each predecessor code back to its current-axis successor for a
    calendar prior-period breakdown: {predecessor -> original window period}.

    Prior-period at an irregular grain filters the fact to the predecessor
    (seq-1) of each period in the window, so a breakdown axis comes back labelled
    with predecessor codes. This inverts that via the same calendar so a
    day/week breakdown under PP aligns onto the requested axis. One small lookup,
    only when the grain is actually a breakdown dimension (the aggregate PP path
    stays a single query)."""
    if ch_client is None or not _SAFE_COLUMN_RE.match(calendar_key):
        return {}
    sql = (
        f"SELECT prev.{calendar_key} AS pred, cur.{calendar_key} AS orig "
        f"FROM {calendar_table} cur "
        f"INNER JOIN {calendar_table} prev ON prev.seq = cur.seq - 1 "
        f"WHERE cur.{calendar_key} >= {{ps:String}} AND cur.{calendar_key} <= {{pe:String}}"
    )
    result = ch_client.query(sql, parameters={"ps": period_start, "pe": period_end})
    return {row[0]: row[1] for row in result.result_rows}


# ---------------------------------------------------------------------------
# Public: retrieve
# ---------------------------------------------------------------------------

def retrieve(
    plan: ExecutionPlan,
    catalogue: Catalogue,
    ch_client,
    dimension_filters: dict[str, list[str]] | None = None,
    ibis_backends: dict[str, object] | None = None,
    ragged_breakdown: dict | None = None,
) -> RawResults:
    """Execute all data queries in the plan and return raw results.

    Merges results from multiple rollup_method groups into a unified result
    per scenario.

    Args:
        plan:              Execution plan from the resolver.
        catalogue:         Loaded catalogue.
        ch_client:         ClickHouse client (clickhouse-connect).
        dimension_filters: Resolved dimension filters {view_col: [leaf_ids]}, or None.

    Returns:
        RawResults: scenario_key -> dimension_key -> metric_key -> value
    """
    raw: RawResults = {}

    # Track which scenarios need time dimension remapping (shifted scenarios)
    # Identify which dimensions in the plan are time-based (period, quarter, fiscal_year)
    _TIME_DIMS = {"period", "quarter", "fiscal_year", "week", "day"}
    time_dim_indices: list[tuple[int, str]] = [
        (i, dim) for i, dim in enumerate(plan.dimensions) if dim in _TIME_DIMS
    ]

    time_remap: dict[str, int] = {}  # scenario_key -> inverse offset (arithmetic)
    # scenario_key -> (breakdown dim, {predecessor -> current}) for calendar PP
    calendar_remap: dict[str, tuple[str, dict[str, str]]] = {}

    for dq in plan.data_queries:
        scenario_key = dq.scenario_key

        if scenario_key not in raw:
            raw[scenario_key] = {}

        if dq.time_offset != 0 and time_dim_indices:
            time_remap[scenario_key] = -dq.time_offset
        elif dq.calendar_table and dq.calendar_key in plan.dimensions:
            # Irregular-grain PP broken down by its own grain: fetch the
            # predecessor -> current mapping so the axis aligns.
            smap = _successor_remap(
                ch_client, dq.calendar_table, dq.calendar_key,
                dq.period_start, dq.period_end,
            )
            if smap:
                calendar_remap[scenario_key] = (dq.calendar_key, smap)

        scenario_results = raw[scenario_key]

        domain = catalogue.domains.get(dq.domain)
        if domain is None:
            raise KeyError(f"Unknown catalogue domain: {dq.domain!r}")

        if domain.backend_kind == "ibis":
            if ibis_backends is None or domain.backend not in ibis_backends:
                raise RuntimeError(
                    f"Domain {dq.domain!r} requires Ibis backend {domain.backend!r}, "
                    "but no connection was provided"
                )
            from precis_mcp.engine.ibis_retriever import (
                execute_ibis_queries,
                rollup_detail_rows,
            )

            result_sets = execute_ibis_queries(
                dq,
                catalogue,
                plan.dimensions,
                dimension_filters,
                ibis_backends[domain.backend],
            )
            result_sets = rollup_detail_rows(
                result_sets[0],
                plan.dimensions,
                dq.metric_keys,
                plan.grains,
            )
        else:
            if ragged_breakdown is not None:
                queries = generate_ragged_sql(
                    dq, catalogue, ragged_breakdown, dimension_filters
                )
            else:
                queries = generate_sql(
                    dq, catalogue, plan.dimensions, dimension_filters, plan.grains
                )
            result_sets = execute_queries(queries, ch_client)

        for result_set in result_sets:
            for row in result_set:
                _merge_row_into_results(
                    scenario_results,
                    row,
                    dq.metric_keys,
                    plan.dimensions,
                )

    # Remap time dimension keys for shifted scenarios so they align
    # with the original requested period range (e.g. 2024-01 → 2025-01,
    # 2024-Q1 → 2025-Q1 for prior_year with time_offset=-12).
    if time_remap or calendar_remap:
        for scenario_key in set(time_remap) | set(calendar_remap):
            inverse_offset = time_remap.get(scenario_key)
            cal = calendar_remap.get(scenario_key)  # (dim_name, {pred -> current})
            old_data = raw.get(scenario_key, {})
            new_data: dict[DimensionKey, dict[str, float | None]] = {}
            for dim_key, metrics in old_data.items():
                parts = list(dim_key)
                for idx, dim_name in time_dim_indices:
                    if parts[idx] == ROLLED_UP:
                        continue
                    if inverse_offset is not None:
                        parts[idx] = _shift_time_value(parts[idx], dim_name, inverse_offset)
                    elif cal is not None and dim_name == cal[0]:
                        parts[idx] = cal[1].get(parts[idx], parts[idx])
                new_data[tuple(parts)] = metrics
            raw[scenario_key] = new_data

    return raw
