# SPDX-License-Identifier: Elastic-2.0
"""Bounded worksheet argument codec; never evaluates Excel expressions."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Callable


class ConversionError(ValueError):
    """A supported, actionable refusal (safe to return to the caller)."""

    def __init__(self, code: str, reason: str, remedy: str = "Open the workbook in Excel with the Précis add-in and correct the selected region."):
        super().__init__(reason)
        self.code, self.reason, self.remedy = code, reason, remedy

    def diagnostic(self) -> dict:
        return {"code": self.code, "reason": self.reason, "remedy": self.remedy}


@dataclass(frozen=True)
class FinancialFormula:
    tool: str
    params: dict
    layout: str


_FUNCTION_PREFIX = r"(?:(?:_xlfn|_xlws|_xludf)\.)*(?:PRECIS\.|_xldudf_PRECIS_)"
FINANCIAL_FUNCTION = re.compile(r"^\s*=?\s*"+_FUNCTION_PREFIX+r"(STATEMENT|METRIC)\s*\((.*)\)\s*$", re.I | re.S)
_PRECIS_CALL = re.compile(r"(?<![\w.])"+_FUNCTION_PREFIX+r"[A-Z][A-Z0-9_]*\s*\(", re.I)
SOURCE_KEYS = frozenset({"statement", "metrics", "period_start", "period_end", "scenarios", "dimensions", "filters", "scale", "decimals"})


def contains_precis_function(formula: str) -> bool:
    """Recognize ordinary and Excel-persisted calls, excluding string literals.

    Detection covers all PRECIS functions so preservation guards also protect
    functions that the financial converter deliberately does not support.
    """
    return bool(_PRECIS_CALL.search(re.sub(r'"(?:[^"]|"")*"', '""', formula)))


def validate_source(source: dict) -> dict:
    if not isinstance(source, dict) or source.get("tool") not in {"run_statement", "run_metric"}:
        raise ConversionError("unsupported_source", "Only statement and metric financial sources can be replayed.")
    params = source.get("params")
    if not isinstance(params, dict) or set(params) - SOURCE_KEYS:
        raise ConversionError("unsupported_arguments", "Source contains unsupported query arguments; regenerate this export.")
    return dict(params)


def _arguments(text: str) -> list[str]:
    tokens, start, quoted = [], 0, False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            quoted = not quoted
        elif char == "," and not quoted:
            tokens.append(text[start:index].strip())
            start = index + 1
        elif char in "();{}" and not quoted:
            raise ConversionError("unsupported_expression", "Formula arguments must be literals or simple scalar cell references/names.")
        index += 1
    if quoted:
        raise ConversionError("invalid_formula", "Formula contains an unterminated string.")
    tokens.append(text[start:].strip())
    return tokens


def parse_financial_formula(formula: str, resolve_scalar: Callable[[str], object]) -> FinancialFormula:
    """Mirror the existing STATEMENT/METRIC signatures and MCP defaults."""
    if len(formula) > 8192:
        raise ConversionError("invalid_formula", "Formula exceeds the supported Excel formula length.")
    match = FINANCIAL_FUNCTION.fullmatch(formula)
    if not match:
        raise ConversionError("unsupported_function", "Only a direct PRECIS.STATEMENT or PRECIS.METRIC anchor can be converted.")
    statement = match[1].upper() == "STATEMENT"
    tokens = _arguments(match[2])
    if not 3 <= len(tokens) <= 9:
        raise ConversionError("invalid_arguments", "The financial function requires 3 to 9 worksheet arguments.")
    values = []
    for token in tokens:
        if not token:
            value = None
        elif re.fullmatch(r'"(?:[^\"]|"")*"', token):
            value = token[1:-1].replace('""', '"')
        elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
            value = float(token)
            if not math.isfinite(value):
                raise ConversionError("invalid_arguments", "Formula contains a non-finite number.")
        else:
            value = resolve_scalar(token)
        values.append(value)
    values += [None] * (9 - len(values))

    def string(index, default=""):
        value = values[index]
        if value is None:
            return default
        if not isinstance(value, str):
            raise ConversionError("invalid_arguments", f"Argument {index + 1} must resolve to text.")
        return value

    def csv(index):
        return [v.strip() for v in string(index).split(",") if v.strip()]

    params = {"period_start": string(1), "period_end": string(2)}
    if any(not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", params[k]) for k in params):
        raise ConversionError("invalid_period", "Use explicit monthly periods in YYYY-MM format.")
    if statement:
        params["statement"] = string(0)
    else:
        params["metrics"] = csv(0)
    if not params.get("statement", params.get("metrics")):
        raise ConversionError("invalid_selection", "The formula has no statement or metric selection.")
    scenarios = []
    for entry in csv(3 if statement else 4):
        parts = re.split(r"\s+as\s+", entry, maxsplit=1, flags=re.I)
        scenarios.append({"scenario": parts[0].strip(), **({"alias": parts[1].strip()} if len(parts) == 2 else {})})
    if scenarios:
        params["scenarios"] = scenarios
    dimensions = csv(4 if statement else 3)
    if dimensions:
        params["dimensions"] = dimensions
    filters = {}
    for entry in csv(5):
        key, sep, value = entry.partition("=")
        parts = [v.strip() for v in value.split("|") if v.strip()]
        if sep and key.strip() and parts:
            filters[key.strip()] = parts if len(parts) > 1 else parts[0]
    if filters:
        params["filters"] = filters
    layout = string(8 if statement else 6, "report").strip().lower() or "report"
    if layout not in {"report", "extract"}:
        raise ConversionError("invalid_presentation", "Use report or extract layout.")
    for key, index, default in [("scale", 6 if statement else 7, 3), ("decimals", 7 if statement else 8, 1)]:
        value = values[index]
        value = default if value is None or value == "" else value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            raise ConversionError("invalid_presentation", f"{key} must be an integer.")
        value = int(value)
        if key == "scale" and value not in {0, 3, 6, 9} or key == "decimals" and not 0 <= value <= 15:
            raise ConversionError("invalid_presentation", f"Unsupported {key} value.")
        params[key] = value
    return FinancialFormula("run_statement" if statement else "run_metric", params, layout)
