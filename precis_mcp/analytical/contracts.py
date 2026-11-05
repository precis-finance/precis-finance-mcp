# SPDX-License-Identifier: Elastic-2.0
"""Provider-neutral contracts for trusted analytical SQL, not finance policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .sql import SqlDialect

Parameters = Mapping[str, Any] | Sequence[Any] | None


class AnalyticalError(RuntimeError):
    """Safe caller-facing error; driver diagnostics must not expose credentials."""


class ConfigurationError(AnalyticalError):
    pass


class DependencyError(AnalyticalError):
    pass


class ExecutionError(AnalyticalError):
    pass


class StatementError(ExecutionError):
    """Driver feedback only; callers own mutation admission and recovery policy."""

    def __init__(self, outcome: str = "unknown", statement_id: str | None = None):
        message = f"Analytical statement outcome: {outcome}; Précis did not resubmit it."
        if statement_id:
            message += f" Statement ID: {statement_id}."
        if outcome == "unknown":
            message += " Reconcile the outcome before retrying."
        super().__init__(message)
        self.outcome = outcome
        self.statement_id = statement_id


class ClientClosedError(AnalyticalError):
    pass


class ReadContractError(AnalyticalError):
    """The queried data violates its declared canonical input contract."""


@dataclass(frozen=True)
class QueryResult:
    """Eager rows retain native Decimal/null/date/timestamp values.

    Type names remain provider-native; business code must not infer roll-up
    grain or missing-value semantics from transport metadata.
    """

    column_names: list[str]
    result_rows: list[tuple[Any, ...]]
    column_types: list[str]
    query_id: str | None = None


class AnalyticalClient(Protocol):
    dialect: SqlDialect
    def query(self, sql: str, parameters: Parameters = None) -> QueryResult: ...
    def execute(self, sql: str, parameters: Parameters = None, *, settings=None) -> QueryResult: ...
    def close(self) -> None: ...


class ManagedClient:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
