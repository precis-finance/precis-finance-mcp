# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Apply `instance/semantic/{dims,views}/*.sql` as CH views.

For each `.sql` file under `instance/semantic/dims/` and `instance/semantic/views/`,
wrap the body with `CREATE OR REPLACE VIEW semantic.<stem> AS <body>` and execute.
Filename stem is the view name.

Idempotent — `CREATE OR REPLACE VIEW` overwrites in place. Safe to
re-run after editing a `.sql` file.

The semantic schema is created if missing.

Apply order
-----------
Dims first, then views — views may reference dims via
`semantic.dim_<x>`, and CH validates the references at view-creation
time. Within each directory, files are applied in sorted order
(deterministic).

File convention
---------------
Each `.sql` file carries a bare `SELECT … FROM live.<x> …` (or any
CH-valid query) without the `CREATE OR REPLACE VIEW …` prefix. The
runner wraps. Leading SQL-comment lines are stripped before wrapping
so the emitted DDL is cosmetically clean (same handling as
`live_ddl_runner._read_body`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.semantic_runner")


@dataclass
class AppliedReport:
    """Per-run summary: which views were applied, in order."""

    schema_ensured: bool = False
    views_applied: list[str] = field(default_factory=list)
    # Just the bare view name (e.g. `dim_account`, `v_gl`). The schema
    # is always `semantic`.


def apply_all(
    instance_semantic_dir: Path,
    ch_client: Any,
    *,
    catalogue: Any = None,
    schema: str = "semantic",
) -> AppliedReport:
    """Apply every `*.sql` body under `instance_semantic_dir/{dims,views}`
    as `CREATE OR REPLACE VIEW {schema}.<stem> AS <body>`.

    `ch_client.command(sql)` is expected to match clickhouse-connect's
    `Client.command`. Raises on any CH error.

    When `catalogue` is supplied, the catalogue-derived ragged-hierarchy
    views are generated in memory and applied between the file-based dims
    and views (see `precis_mcp.engine.ragged_views`). Order is load-bearing:
    file dims first (a ragged dim's leaf source may be a `semantic.dim_*`
    file view), then the ragged views, then views (which may reference the
    ragged views). CH validates references at CREATE VIEW time.
    """
    report = AppliedReport()

    ch_client.command(f"CREATE DATABASE IF NOT EXISTS {schema}")
    report.schema_ensured = True
    _logger.info("ingestion.semantic.schema_ensured", schema=schema)

    _apply_sql_dir(instance_semantic_dir / "dims", ch_client, schema, report)

    if catalogue is not None:
        from precis_mcp.engine.passthrough_views import build_passthrough_views
        from precis_mcp.engine.ragged_views import build_ragged_views

        # Pass-throughs for catalogue dims with no operator-authored file, then
        # ragged views (which may read those pass-throughs, e.g. a rollup whose
        # leaf source is an auto-generated semantic.dim_*). Both are dims-tier,
        # so they land after file dims and before file views.
        file_dims = set(report.views_applied)
        for name, body in build_passthrough_views(catalogue, file_dims):
            stmt = f"CREATE OR REPLACE VIEW {schema}.{name} AS\n{_clean_body(body)}"
            ch_client.command(stmt)
            report.views_applied.append(name)
            _logger.info(
                "ingestion.semantic.passthrough_view_applied",
                schema=schema,
                view=name,
            )

        for name, body in build_ragged_views(catalogue):
            stmt = f"CREATE OR REPLACE VIEW {schema}.{name} AS\n{_clean_body(body)}"
            ch_client.command(stmt)
            report.views_applied.append(name)
            _logger.info(
                "ingestion.semantic.ragged_view_applied",
                schema=schema,
                view=name,
            )

    _apply_sql_dir(instance_semantic_dir / "views", ch_client, schema, report)

    return report


def _apply_sql_dir(path: Path, ch_client: Any, schema: str, report: AppliedReport) -> None:
    """Apply every `*.sql` file under `path` (sorted) as a CH view."""
    if not path.exists():
        _logger.warning(
            "ingestion.semantic.subdir_missing",
            subdir=path.name,
            path=str(path),
        )
        return
    for sql_file in sorted(path.glob("*.sql")):
        name = sql_file.stem
        body = _read_body(sql_file)
        stmt = f"CREATE OR REPLACE VIEW {schema}.{name} AS\n{body}"
        ch_client.command(stmt)
        report.views_applied.append(name)
        _logger.info(
            "ingestion.semantic.view_applied",
            schema=schema,
            view=name,
            source_file=str(sql_file),
        )


def _read_body(path: Path) -> str:
    """Read a body file and clean it (strip leading comment/blank lines and
    trailing whitespace + semicolons). Mirrors `live_ddl_runner._read_body`."""
    return _clean_body(path.read_text(encoding="utf-8"))


def _clean_body(text: str) -> str:
    """Strip leading comment-only / blank lines and trailing whitespace +
    semicolons from a SQL body (file-sourced or generated in memory)."""
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            start = i
            break
    body = "\n".join(lines[start:]).rstrip()
    return body.rstrip(";").rstrip()