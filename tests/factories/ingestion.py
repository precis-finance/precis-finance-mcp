# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Domain-object builders for ingestion tests.

Produces valid `Source` and `Binding` YAML payloads, plus a full on-disk
registry tree under a `tmp_path`. Tests pass these into
`IntegrationRegistry.load(...)`.

Post-refactor shape:
- `Source.kind` is `postgres` or `http_upload` (the two kinds the
  Ibis-driven pipeline currently supports).
- `Binding` declares `target` (e.g. `'live.fact_gl'`), `kind`
  (`period` or `snapshot`), and an `extract.query` (operator-authored
  SQL). No more `dataset`, `partition_expression`, `order_by`, or
  dbt schema.yml coupling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml


__all__ = [
    "build_tree",
    "make_source",
    "make_binding",
    "write_yaml",
    "seed_matching_shapes",
    "make_orchestrator_context",
    "DEFAULT_LANDING_COLUMNS",
]


DEFAULT_LANDING_COLUMNS = [
    ("period", "String"),
    ("account_code", "String"),
    ("cost_centre", "String"),
    ("amount", "Decimal(18, 2)"),
    ("_load_id", "String"),
    ("_ingested_at", "DateTime"),
]


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` as YAML, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_source(
    source_id: str = "test_pg",
    kind: str = "postgres",
) -> dict[str, Any]:
    """A valid `Source` YAML dict.

    Defaults to a `postgres` source — matches the production pattern
    where bindings read aggregations from a customer warehouse via
    Ibis. Pass `kind='http_upload'` for file-drop tests.
    """
    base: dict[str, Any] = {
        "id": source_id,
        "display_name": f"Test {kind} source ({source_id})",
        "kind": kind,
        "secret_ref": source_id,
        "network": {
            "egress_required": kind != "http_upload",
            "endpoints": [],
        },
        "backend": {},
        "metadata": {},
    }
    if kind == "http_upload":
        base["backend"] = {
            "file_format": "csv",
            "prefix": "test/",
            "csv": {
                "delimiter": ",",
                "encoding": "utf-8",
                "has_header": True,
                "quotechar": '"',
            },
        }
    return base


def make_binding(
    binding_id: str = "test_pg__fact_gl",
    source: str = "test_pg",
    target: str = "live.fact_gl",
    kind: str = "period",
    extract_query: Optional[str] = None,
    schedule_mode: str = "push",
    scenario: str = "ACTUALS",
) -> dict[str, Any]:
    """A valid `Binding` YAML dict in the post-refactor shape.

    Default is a period binding pushed by an operator with a minimal GL
    extract query. Pass `kind='snapshot'` for snapshot bindings (the
    query should then omit the `:period` filter).
    """
    if extract_query is None:
        if kind == "period":
            extract_query = (
                "SELECT period, account_code, cost_centre, amount "
                "FROM gl.journal_postings WHERE period = :period"
            )
        else:
            extract_query = (
                "SELECT account_code, account_name, account_type, fs_line "
                "FROM gl.accounts"
            )

    schedule: dict[str, Any] = {"mode": schedule_mode}
    if schedule_mode == "push":
        schedule["push_auth"] = {
            "role": "ingest_admin",
            "binding_token_ref": f"{source}_token",
        }
    elif schedule_mode == "cron":
        schedule.update({"expression": "0 2 * * *", "timezone": "UTC"})
    elif schedule_mode == "watch":
        schedule["watch"] = {
            "file_glob": "*.csv",
            "period_from": "filename_regex",
            "filename_regex": r".*_(?P<period>\d{4}-\d{2})\.csv",
        }
    if kind == "period":
        schedule["period_selection"] = {
            "strategy": "lookback",
            "lookback_periods": 3,
        }

    return {
        "id": binding_id,
        "source": source,
        "target": target,
        "scenario": scenario,
        "kind": kind,
        "schedule": schedule,
        "extract": {"query": extract_query},
    }


def build_tree(
    tmp_path: Path,
    sources: Optional[list[dict[str, Any]]] = None,
    bindings: Optional[list[dict[str, Any]]] = None,
) -> Path:
    """Write a valid integration registry directory under
    `tmp_path / "integrations"`.

    Returns the root path you pass to `IntegrationRegistry.load(...)`.
    Defaults to a single canonical source/binding pair.
    """
    root = tmp_path / "integrations"
    src_list = sources if sources is not None else [make_source()]
    bd_list = bindings if bindings is not None else [make_binding()]

    for src in src_list:
        write_yaml(root / "sources" / f"{src['id']}.yml", src)
    for bd in bd_list:
        write_yaml(root / "bindings" / f"{bd['id']}.yml", bd)

    return root


def seed_matching_shapes(ch: Any) -> None:
    """Seed a `FakeChClient` so `validate_staging_shape` sees identical
    columns on `live.*` and `staging.*` — validation passes."""
    ch.set_response("database = 'live'", DEFAULT_LANDING_COLUMNS)
    ch.set_response("database = 'staging'", DEFAULT_LANDING_COLUMNS)


def make_orchestrator_context(
    registry: Any,
    *,
    ch_client: Any = None,
    ibis_backend: Any = None,
    lock_available: bool = True,
    source_path: Optional[str] = None,
    clock: Any = None,
) -> tuple[Any, Any, Any, Any]:
    """Build a current-pipeline `OrchestratorContext` + its load-history fake.

    Defaults land a 3-row DataFrame through a `FakeIbisBackend` and seed the
    `FakeChClient` so the validate stage passes — a full `run_binding`
    succeeds. Override `ch_client` / `ibis_backend` to script a stage
    failure. Returns `(ctx, history, ch_client, ibis_backend)`.
    """
    import pandas as pd

    from precis_mcp.ingestion.orchestrator import OrchestratorContext
    from tests.fakes.ingestion import (
        FakeChClient,
        FakeIbisBackend,
        FakeLoadHistoryWriter,
        FakeLockFactory,
    )

    if ch_client is None:
        ch_client = FakeChClient()
        seed_matching_shapes(ch_client)
    if ibis_backend is None:
        ibis_backend = FakeIbisBackend(df=pd.DataFrame({
            "period": ["2026-04", "2026-04", "2026-04"],
            "account_code": ["1100", "1100", "1200"],
            "cost_centre": ["CC-01", "CC-02", "CC-01"],
            "amount": [100.00, 200.00, 300.00],
        }))
    history = FakeLoadHistoryWriter(clock=clock)
    ctx = OrchestratorContext(
        registry=registry,
        load_history=history,
        lock_factory=FakeLockFactory(available=lock_available),
        ch_client=ch_client,
        ibis_backend_for_source=lambda _src: ibis_backend,
        source_path_resolver=lambda _src: source_path,
    )
    return ctx, history, ch_client, ibis_backend
