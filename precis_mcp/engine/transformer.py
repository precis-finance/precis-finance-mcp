# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import ast
import operator
from typing import TYPE_CHECKING

from precis_mcp.engine.catalogue import DerivedMetric
from precis_mcp.engine.types import DimensionKey, ResultData

if TYPE_CHECKING:
    from precis_mcp.engine.catalogue import Catalogue


# ---------------------------------------------------------------------------
# Expression evaluator
# ---------------------------------------------------------------------------

class ExpressionError(Exception):
    """Raised when an expression contains unsafe or unsupported constructs."""
    pass


def _eval_node(node: ast.expr, variables: dict[str, float | None]) -> float | None:
    """Recursively evaluate an AST expression node.

    Returns a float or None (for None propagation or division by zero).
    Raises ExpressionError for unsupported/unsafe constructs.
    """
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ExpressionError(f"Unsupported constant type: {type(node.value)!r}")
        return float(node.value)

    elif isinstance(node, ast.Name):
        if node.id not in variables:
            raise ExpressionError(f"Unknown variable: {node.id!r}")
        val = variables[node.id]
        # None propagates
        return val if val is None else float(val)

    elif isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            operand = _eval_node(node.operand, variables)
            if operand is None:
                return None
            return -operand
        elif isinstance(node.op, ast.UAdd):
            operand = _eval_node(node.operand, variables)
            return operand
        else:
            raise ExpressionError(
                f"Unsupported unary operator: {type(node.op).__name__!r}"
            )

    elif isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)

        if left is None or right is None:
            return None

        if isinstance(node.op, ast.Add):
            return left + right
        elif isinstance(node.op, ast.Sub):
            return left - right
        elif isinstance(node.op, ast.Mult):
            return left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                return None
            return left / right
        else:
            raise ExpressionError(
                f"Unsupported binary operator: {type(node.op).__name__!r}"
            )

    elif isinstance(node, ast.Call):
        # Only allow abs() with exactly one argument
        if not isinstance(node.func, ast.Name):
            raise ExpressionError(
                "Function calls with attribute access are not allowed"
            )
        func_name = node.func.id
        if func_name != "abs":
            raise ExpressionError(
                f"Unsupported function: {func_name!r}. Only 'abs' is allowed."
            )
        if len(node.args) != 1 or node.keywords:
            raise ExpressionError("abs() requires exactly one positional argument")
        arg = _eval_node(node.args[0], variables)
        if arg is None:
            return None
        return abs(arg)

    elif isinstance(node, ast.IfExp):
        # Supports: "expr if condition else fallback"
        # Condition is truthy when non-None and non-zero.
        test_val = _eval_node(node.test, variables)
        if test_val is not None and test_val != 0:
            return _eval_node(node.body, variables)
        else:
            return _eval_node(node.orelse, variables)

    elif isinstance(node, ast.Attribute):
        raise ExpressionError(
            "Attribute access is not allowed in expressions"
        )

    else:
        raise ExpressionError(
            f"Unsupported AST node type: {type(node).__name__!r}"
        )


def evaluate_expression(formula: str, variables: dict[str, float | None]) -> float | None:
    """Safely evaluate an arithmetic expression with variable substitution.

    Supports: +, -, *, /, parentheses, numeric literals, abs(), variable references.
    Division by zero returns None.
    Any operation involving None returns None (None propagation).

    Args:
        formula: Expression string, e.g. "revenue + direct_cost"
        variables: Variable name -> value mapping

    Returns:
        Computed value, or None if division by zero or None propagation

    Raises:
        ExpressionError: If the formula contains unsafe or unsupported constructs
        SyntaxError: If the formula is not valid Python syntax
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid formula syntax: {formula!r} — {exc}") from exc

    # Check for disallowed top-level constructs via walk — catches Import, etc.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ExpressionError("Import statements are not allowed in expressions")
        # ast.parse in 'eval' mode won't produce statements, but just in case:
        if isinstance(node, ast.Attribute):
            raise ExpressionError("Attribute access is not allowed in expressions")

    return _eval_node(tree.body, variables)


# ---------------------------------------------------------------------------
# Derived metrics computation
# ---------------------------------------------------------------------------

def compute_derived_metrics(
    scenario_data: dict[DimensionKey, dict[str, float | None]],
    catalogue: Catalogue,
    metric_keys: list[str],
) -> None:
    """Compute derived metrics in-place for a single scenario's data.

    Args:
        scenario_data: Mutable dict of dimension_key -> {metric_key: value}
        catalogue: For looking up metric formulas
        metric_keys: All metric keys to compute (must be topologically sorted —
                     base metrics first, then derived in dependency order)

    Modifies scenario_data in-place: for each dimension_key, adds derived metric values.
    """
    # Filter to only derived metrics (preserving order for dependency correctness)
    derived_keys = [
        key for key in metric_keys
        if key in catalogue.metrics and isinstance(catalogue.metrics[key], DerivedMetric)
    ]

    if not derived_keys:
        return

    for dim_key, metric_dict in scenario_data.items():
        for metric_key in derived_keys:
            derived = catalogue.metrics[metric_key]
            assert isinstance(derived, DerivedMetric)
            # Build variables from all currently computed values for this dim_key
            variables: dict[str, float | None] = {
                k: v for k, v in metric_dict.items()
            }
            result = evaluate_expression(derived.formula, variables)
            metric_dict[metric_key] = result


# ---------------------------------------------------------------------------
# Computed scenarios
# ---------------------------------------------------------------------------

def compute_scenario(
    results: ResultData,
    scenario_key: str,
    formula: str,
    metric_keys: list[str],
) -> None:
    """Compute a computed scenario in-place.

    For each metric in metric_keys, applies the scenario formula using values
    from referenced scenarios.

    E.g., formula "actuals - budget" with metric "revenue":
    result = results['actuals'][dim_key]['revenue'] - results['budget'][dim_key]['revenue']

    Args:
        results: Mutable ResultData — modifies in-place
        scenario_key: Key for the new scenario (e.g. 'budget_variance')
        formula: Scenario formula (e.g. 'actuals - budget')
        metric_keys: All metrics to compute for this scenario
    """
    # Collect all dimension keys from all existing scenarios
    all_dim_keys: set[DimensionKey] = set()
    for scen_data in results.values():
        all_dim_keys.update(scen_data.keys())

    # Initialise the new scenario
    if scenario_key not in results:
        results[scenario_key] = {}

    for dim_key in all_dim_keys:
        if dim_key not in results[scenario_key]:
            results[scenario_key][dim_key] = {}

        for metric_key in metric_keys:
            # Build variables: scenario_name -> value for this metric at this dim_key
            variables: dict[str, float | None] = {}
            for scen_name, scen_data in results.items():
                if scen_name == scenario_key:
                    continue
                dim_metrics = scen_data.get(dim_key, {})
                variables[scen_name] = dim_metrics.get(metric_key)

            result = evaluate_expression(formula, variables)
            results[scenario_key][dim_key][metric_key] = result


# ---------------------------------------------------------------------------
# Main transform function
# ---------------------------------------------------------------------------

def transform(
    raw_results: ResultData,
    catalogue: Catalogue,
    computed_evals: list,  # list of ComputedScenarioEval from resolver
    metric_keys: list[str],  # all metric keys needed, topologically sorted
) -> ResultData:
    """Complete the transformation stage.

    1. Compute derived metrics within each scenario
    2. Compute computed scenarios cross-scenario

    Returns the enriched results (same dict, modified in-place).
    """
    # Stage 1: Compute derived metrics within each real (non-computed) scenario
    for scenario_key, scenario_data in raw_results.items():
        compute_derived_metrics(scenario_data, catalogue, metric_keys)

    # Stage 2: Compute each computed scenario in topological order.
    # compute_scenario iterates every metric_key (base AND derived) and applies
    # the scenario formula to the already-derived values in the source scenarios.
    # For ratio metrics this is the only correct semantics — re-deriving from
    # subtracted base inputs gives (A−B)/(C−D) instead of A/C − B/D.
    for eval_item in computed_evals:
        compute_scenario(
            results=raw_results,
            scenario_key=eval_item.scenario_key,
            formula=eval_item.formula,
            metric_keys=metric_keys,
        )

    return raw_results
