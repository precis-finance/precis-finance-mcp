# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared base for file-format readers."""

from __future__ import annotations

from typing import Any, BinaryIO, Iterable, Mapping, Protocol, Union


# Source-side column name → dataset-side column name. The literal "identity"
# means "source already uses dataset-canonical names verbatim."
ColumnMap = Union[Mapping[str, str], str]


class FileReader(Protocol):
    """Reads one file, applies column_map, yields rows with dataset-canonical
    column names. Format-specific config (delimiter, sheet name, encoding,
    etc.) is passed as a flat dict."""

    format_name: str  # 'csv' | 'parquet' | 'xlsx'

    def read(
        self,
        file_data: Union[bytes, BinaryIO],
        *,
        format_config: dict[str, Any],
        column_map: ColumnMap,
    ) -> Iterable[dict[str, Any]]: ...


class FileReadError(Exception):
    """Raised when a file cannot be parsed or its columns can't be mapped
    onto the dataset's canonical names."""


def apply_column_map(
    raw_row: dict[str, Any],
    column_map: ColumnMap,
) -> dict[str, Any]:
    """Apply the binding's column_map to one parsed row.

    `identity` mode passes the row through unchanged (source already uses
    canonical names). Dict mode renames keys: each declared source field maps
    to one dataset-canonical name; unmapped source fields are dropped.
    """
    if column_map == "identity":
        return dict(raw_row)
    if not isinstance(column_map, Mapping):
        raise FileReadError(
            f"column_map must be a mapping or 'identity', got {type(column_map).__name__}"
        )
    out: dict[str, Any] = {}
    for source_col, dataset_col in column_map.items():
        if source_col in raw_row:
            out[dataset_col] = raw_row[source_col]
    return out
