# SPDX-License-Identifier: Elastic-2.0
"""Small SQL rendering helpers used at construction time, never transpilation.

The shared engine still decides grain, periods, scopes and arithmetic. This
module supplies only namespace qualification, casts and native bound markers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
import re

from .config import quote_identifier
from .contracts import ConfigurationError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class SqlDialect:
    backend: str = "clickhouse"
    catalog: str | None = None
    schemas: tuple[tuple[str, str], ...] = (("semantic", "semantic"), ("planning", "precis_planning"),
                                           ("live", "actuals"), ("actuals", "actuals"))
    column_type: Callable[[str, str], tuple[str, bool]] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.backend not in {"clickhouse", "databricks"}:
            raise ConfigurationError("Unsupported SQL dialect.")
        if self.catalog:
            quote_identifier(self.catalog)
        roles = dict(self.schemas)
        if len(roles) != len(self.schemas):
            raise ConfigurationError("Duplicate analytical schema role.")
        for role, physical in self.schemas:
            quote_identifier(role)
            quote_identifier(physical)

    def table(self, logical_name: str) -> str:
        parts = logical_name.split(".")
        if any(not _NAME.fullmatch(part) for part in parts):
            raise ConfigurationError("Invalid logical analytical object name.")
        if self.backend == "clickhouse":
            return logical_name
        if len(parts) != 2 or not self.catalog:
            raise ConfigurationError("Databricks engine SQL requires DATABRICKS_CATALOG and role.table references.")
        schema = dict(self.schemas).get(parts[0])
        if schema is None:
            raise ConfigurationError("Analytical object uses an unmapped schema role.")
        return ".".join(quote_identifier(part) for part in (self.catalog, schema, parts[1]))

    def parameter(self, name: str, ch_type: str = "String") -> str:
        if not _NAME.fullmatch(name):
            raise ConfigurationError("Invalid analytical parameter name.")
        return f"{{{name}:{ch_type}}}" if self.backend == "clickhouse" else f":{name}"

    def replace_table_as(self, logical_name: str, query: str) -> str:
        """Operator-only full replacement; readers retain the previous table on failure."""
        storage = "ENGINE = MergeTree ORDER BY tuple()" if self.backend == "clickhouse" else "USING DELTA"
        return f"CREATE OR REPLACE TABLE {self.table(logical_name)} {storage} AS\n{query}"

    def string(self, expression: str) -> str:
        return f"toString({expression})" if self.backend == "clickhouse" else f"CAST({expression} AS STRING)"

    def boolean(self, value: bool, *, relation: str | None = None, source_column: str | None = None) -> str:
        if self.backend == "clickhouse":
            return "1" if value else "0"
        if source_column is not None:
            if self.column_type is None or relation is None:
                raise ConfigurationError("Native Boolean predicates require warehouse column metadata.")
            native_type, _ = self.column_type(relation, source_column)
            if native_type in {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT"}:
                return "1" if value else "0"
            if native_type != "BOOLEAN":
                raise ConfigurationError("Boolean predicate requires a Boolean or integer flag column.")
        return "TRUE" if value else "FALSE"

    def ilike(self, expression: str, parameter: str) -> str:
        marker = self.parameter(parameter)
        if self.backend == "clickhouse":
            return f"ilike({expression}, {marker})"
        return f"{expression} ILIKE {marker}"

    def grouping_mask(self, expressions: list[str]) -> str:
        function = "GROUPING" if self.backend == "clickhouse" else "GROUPING_ID"
        return f"{function}({', '.join(expressions)})"

    def array(self, expressions: list[str]) -> str:
        values = ", ".join(expressions)
        return f"[{values}]" if self.backend == "clickhouse" else f"ARRAY({values})"

    def array_append(self, array: str, value: str) -> str:
        function = "arrayPushBack" if self.backend == "clickhouse" else "array_append"
        return f"{function}({array}, {value})"

    def array_contains(self, array: str, value: str) -> str:
        function = "has" if self.backend == "clickhouse" else "array_contains"
        return f"{function}({array}, {value})"

    def count_rows(self) -> str:
        return "count()" if self.backend == "clickhouse" else "COUNT(*)"

    def sum(self, expression: str) -> str:
        """One shared rule: zero for empty input; retain NULL for all-null rows."""
        return f"CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM({expression}) END"

    def require_nonnull(self, expression: str) -> str:
        return (f"CASE WHEN ({expression}) IS NULL THEN "
                f"raise_error('PRECIS_READ_CONTRACT_VIOLATION') ELSE {expression} END")

    def joined_attribute(self, expression: str, relation: str, column: str, *,
                         joined_key: str | None = None) -> str:
        if self.backend == "clickhouse":
            return expression
        if self.column_type is None:
            raise ConfigurationError("Native dimension SQL requires warehouse column metadata.")
        native_type, nullable = self.column_type(relation, column)
        if nullable:
            return expression
        if not joined_key:
            raise ConfigurationError("A joined-key presence expression is required for native dimension defaults.")
        default = {"STRING":"''", "BOOLEAN":"FALSE", "DATE":"DATE '1970-01-01'",
                   "TIMESTAMP":"TIMESTAMP '1970-01-01 00:00:00'"}.get(native_type)
        if default is None:
            if native_type not in {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE"} and not native_type.startswith("DECIMAL("):
                raise ConfigurationError("Unsupported dimension column type.")
            default = "0"
        return (f"CASE WHEN {joined_key} IS NULL THEN {default} "
                f"ELSE {self.require_nonnull(expression)} END")

    def in_values(self, expression: str, name: str, values: list[str], parameters: dict) -> str:
        """Bind every list element; an empty resolved scope always denies all."""
        if not _NAME.fullmatch(name):
            raise ConfigurationError("Invalid analytical parameter name.")
        if self.backend == "clickhouse":
            parameters[name] = values
            return f"{expression} IN ({self.parameter(name, 'Array(String)')})"
        if not values:
            return "0 = 1"
        markers = []
        for index, value in enumerate(values):
            key = f"{name}_{index}"
            if key in parameters:
                raise ConfigurationError("Analytical list parameter collision.")
            parameters[key] = value
            markers.append(self.parameter(key))
        return f"{expression} IN ({', '.join(markers)})"


CLICKHOUSE_SQL = SqlDialect()


def dialect_for(client) -> SqlDialect:
    """Legacy raw CH clients and existing fakes keep the original rendering."""
    candidate = getattr(client, "dialect", None)
    if candidate is not None and not isinstance(candidate, SqlDialect):
        # Existing unrestricted mocks may synthesise attributes; real objects
        # declaring malformed dialect configuration must not silently use CH.
        if "dialect" in getattr(client, "__dict__", {}) or hasattr(type(client), "dialect"):
            raise ConfigurationError("Analytical client declares an invalid SQL dialect.")
    return candidate if isinstance(candidate, SqlDialect) else CLICKHOUSE_SQL
