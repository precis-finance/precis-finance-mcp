# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""File-format readers — csv, parquet, xlsx.

Each reader parses one file into rows already shaped to the dataset's
canonical column names (after applying `binding.column_map`). The
source-kind drivers (s3, sftp, http_upload) compose the appropriate reader
based on `source.backend.file_format`.

Readers are *parsers*, not drivers — they don't talk to ClickHouse, don't
know about staging tables, don't read `load_history`. They just turn bytes
into a row stream the driver can pass to `clickhouse-connect`'s bulk insert.
"""

from __future__ import annotations

from precis_mcp.ingestion.file_readers.base import FileReader
from precis_mcp.ingestion.file_readers.csv_reader import CsvReader
from precis_mcp.ingestion.file_readers.parquet_reader import ParquetReader
from precis_mcp.ingestion.file_readers.xlsx_reader import XlsxReader


_BUILTIN_READERS: dict[str, FileReader] = {
    "csv": CsvReader(),
    "parquet": ParquetReader(),
    "xlsx": XlsxReader(),
}


def get_reader(format_name: str) -> FileReader:
    """Return the reader for one of the built-in formats. Raises KeyError
    when the format name is unknown."""
    try:
        return _BUILTIN_READERS[format_name]
    except KeyError as exc:
        raise KeyError(
            f"Unknown file format: {format_name!r}; "
            f"known: {sorted(_BUILTIN_READERS)}"
        ) from exc


__all__ = ["FileReader", "CsvReader", "ParquetReader", "XlsxReader", "get_reader"]
