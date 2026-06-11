# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Apply `instance/live/*.sql` DDL to ClickHouse.

For each `instance/live/<name>.sql` file, creates two tables with
identical engine spec — one in `live.<name>` (the read target the
engine queries; the swap promotes into here) and one in
`staging.<name>` (the write target the Ibis executor lands into before
validation + swap).

ClickHouse requires the source and target of `REPLACE PARTITION` to
share engine, PARTITION BY and ORDER BY exactly. Applying the same body
to both schemas is the simplest way to keep that invariant true.

Idempotent: uses `CREATE TABLE IF NOT EXISTS`. Re-running is safe; the
runner does not detect or repair drift — that's a follow-up concern.

The `live` and `staging` schemas (CH databases) are created if missing.

File convention
---------------
Each `.sql` file in `instance/live/` carries the bare body — column
list + engine spec — without `CREATE TABLE …` or a schema qualifier.
The runner wraps:

    CREATE TABLE IF NOT EXISTS <schema>.<filename-stem> <body>

Filename stem is the table name. Comments inside the body pass through
verbatim — ClickHouse tolerates SQL comments mid-statement. Trailing
semicolons / whitespace are stripped before wrapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.live_ddl_runner")


@dataclass
class AppliedReport:
    """Per-run summary of what the runner touched.

    `CREATE IF NOT EXISTS` is opaque to the client about "created" vs
    "already existed" — this report records what was *attempted*, which
    is what tests and operator audit logs need.
    """

    schemas_ensured: list[str] = field(default_factory=list)
    tables_applied: list[tuple[str, str]] = field(default_factory=list)
    # (schema, table_name) in the order applied — useful for tests
    # asserting the schema × file matrix.


def apply_all(
    instance_live_dir: Path,
    ch_client: Any,
    *,
    schemas: tuple[str, ...] = ("live", "staging"),
) -> AppliedReport:
    """Apply every `*.sql` body in `instance_live_dir` as CREATE TABLE
    IF NOT EXISTS in each of `schemas`.

    `ch_client.command(sql)` is expected to match clickhouse-connect's
    `Client.command` — issues DDL/DML, raises on error.

    Raises on any CREATE failure (does not partial-apply silently).
    """
    report = AppliedReport()

    for schema in schemas:
        ch_client.command(f"CREATE DATABASE IF NOT EXISTS {schema}")
        report.schemas_ensured.append(schema)
        _logger.info("ingestion.live_ddl.schema_ensured", schema=schema)

    sql_files = sorted(instance_live_dir.glob("*.sql"))
    if not sql_files:
        _logger.warning(
            "ingestion.live_ddl.no_files",
            instance_live_dir=str(instance_live_dir),
        )
        return report

    for sql_file in sql_files:
        name = sql_file.stem
        body = _read_body(sql_file)
        for schema in schemas:
            stmt = f"CREATE TABLE IF NOT EXISTS {schema}.{name} {body}"
            ch_client.command(stmt)
            report.tables_applied.append((schema, name))
            _logger.info(
                "ingestion.live_ddl.table_applied",
                schema=schema,
                table=name,
                source_file=str(sql_file),
            )

    return report


def _read_body(path: Path) -> str:
    """Read a body file and return just the SQL — columns + engine spec.

    Strips leading comment-only / blank lines so they don't end up on
    the same line as the wrapped `CREATE TABLE IF NOT EXISTS <schema>.<name>`
    prefix (would be valid CH SQL but cosmetically awful in logs and
    errors). The file's leading comment block describes the table for
    humans reading the file; it doesn't belong inside the CH statement.

    Comments deeper in the body (e.g. inline notes between columns) are
    preserved — ClickHouse tolerates `--` comments between SQL tokens.

    Trailing whitespace and any trailing `;` are stripped.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            start = i
            break
    body = "\n".join(lines[start:]).rstrip()
    return body.rstrip(";").rstrip()