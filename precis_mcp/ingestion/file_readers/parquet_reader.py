# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Parquet reader (pyarrow-backed)."""

from __future__ import annotations

import io
from typing import Any, BinaryIO, Iterable, Union

from precis_mcp.ingestion.file_readers.base import (
    ColumnMap,
    FileReadError,
    apply_column_map,
)


class ParquetReader:
    """Single-file Parquet via pyarrow.

    Accepted `format_config` keys:
      - `columns` — optional projection (list of source column names). When
        present, only these columns are pulled from the parquet file.

    Type fidelity is preserved by pyarrow's conversion to Python — Decimal
    fields stay as `decimal.Decimal`, dates stay as `datetime.date`. The
    downstream insert path (clickhouse-connect) handles the conversion.
    """

    format_name = "parquet"

    def read(
        self,
        file_data: Union[bytes, BinaryIO],
        *,
        format_config: dict[str, Any],
        column_map: ColumnMap,
    ) -> Iterable[dict[str, Any]]:
        try:
            import pyarrow.parquet as pq  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — pyarrow is in requirements
            raise FileReadError(
                "parquet reader requires pyarrow; install pyarrow"
            ) from exc

        if isinstance(file_data, (bytes, bytearray)):
            source: Any = io.BytesIO(file_data)
        else:
            source = file_data

        columns = format_config.get("columns")
        try:
            table = pq.read_table(source, columns=columns)
        except Exception as exc:
            raise FileReadError(f"parquet parse failure: {exc}") from exc

        # to_pylist() materialises rows as dicts of Python natives.
        out: list[dict[str, Any]] = []
        for raw_row in table.to_pylist():
            out.append(apply_column_map(raw_row, column_map))
        return out
