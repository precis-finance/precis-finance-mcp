# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""CSV reader."""

from __future__ import annotations

import csv
import io
from typing import Any, BinaryIO, Iterable, Union

from precis_mcp.ingestion.file_readers.base import (
    ColumnMap,
    FileReadError,
    apply_column_map,
)


class CsvReader:
    """RFC-4180-style CSV via the stdlib `csv` module.

    Accepted `format_config` keys:
      - `delimiter` (default ",")
      - `encoding` (default "utf-8")
      - `has_header` (default True). When False, `format_config["columns"]`
        must be a list of column names in file order.
      - `null_marker` — values equal to this become None
      - `quotechar` (default '"')
    """

    format_name = "csv"

    def read(
        self,
        file_data: Union[bytes, BinaryIO],
        *,
        format_config: dict[str, Any],
        column_map: ColumnMap,
    ) -> Iterable[dict[str, Any]]:
        encoding = format_config.get("encoding", "utf-8")
        if isinstance(file_data, (bytes, bytearray)):
            text = file_data.decode(encoding)
            stream = io.StringIO(text)
        else:
            stream = io.TextIOWrapper(file_data, encoding=encoding, newline="")

        delimiter = format_config.get("delimiter", ",")
        quotechar = format_config.get("quotechar", '"')
        has_header = format_config.get("has_header", True)
        null_marker = format_config.get("null_marker")

        if has_header:
            reader = csv.DictReader(
                stream, delimiter=delimiter, quotechar=quotechar
            )
        else:
            columns = format_config.get("columns")
            if not columns:
                raise FileReadError(
                    "csv reader: has_header=False requires 'columns' in format_config"
                )
            reader = csv.DictReader(
                stream,
                fieldnames=list(columns),
                delimiter=delimiter,
                quotechar=quotechar,
            )

        out: list[dict[str, Any]] = []
        for raw_row in reader:
            if null_marker is not None:
                raw_row = {
                    k: (None if v == null_marker else v)
                    for k, v in raw_row.items()
                }
            out.append(apply_column_map(raw_row, column_map))
        return out
