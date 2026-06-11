#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Run a single (binding, period) load up to a specific stage.

Thin operator wrapper around `precis_mcp.ingestion.orchestrator.run_binding`
with `stop_after=<stage>`. Use this to validate one stage of the
ingestion pipeline at a time without rolling forward into the next:

    # Extract only (source → staging.<x>)
    python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04 --stop-after extract

    # Through validate (extract + shape check, no swap)
    python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04 --stop-after validate

    # Through swap (staging → live)
    python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04 --stop-after swap

    # Full pipeline (same as --stop-after swap; swap is the last stage)
    python scripts/run_ingest_stage.py --binding customer_pg__gl --period 2026-04

Snapshot bindings (`kind: snapshot`) take no --period:

    python scripts/run_ingest_stage.py --binding customer_pg__dim_client --stop-after extract

The script wires the full production OrchestratorContext (real CH +
Postgres + Redis + Ibis backends) — identical to what cron / push /
watch triggers use — so a successful stage here is a real signal that
the production path works. There is no dry-run mode; rerun an earlier
period or use a separate scenario if you need to keep the live data
untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `precis` importable when run from the repo root without an
# installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from precis_mcp.ingestion.orchestrator import run_binding  # noqa: E402
from precis_mcp.ingestion.wiring import (  # noqa: E402
    build_default_ingestion_context_from_env,
)


_STAGES = ("extract", "validate", "swap")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one (binding, period) load through the ingestion pipeline, "
                    "optionally stopping at a named stage.",
    )
    parser.add_argument(
        "--binding",
        required=True,
        help="Binding id (e.g. customer_pg__gl).",
    )
    parser.add_argument(
        "--period",
        default=None,
        help="Accounting period in 'YYYY-MM' form (or adjustment-period "
             "token like '2026-13'). Required for kind='period' bindings, "
             "omit for kind='snapshot'.",
    )
    parser.add_argument(
        "--stop-after",
        choices=_STAGES,
        default=None,
        help=(
            "Stop after the named stage. Default: run the full pipeline. "
            "Stages run in order extract → validate → swap."
        ),
    )
    parser.add_argument(
        "--triggered-by",
        default="ops:manual",
        help="Trigger label written to load_history.triggered_by. "
             "Defaults to 'ops:manual'.",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Operator-supplied note recorded on load_history.notes.",
    )
    args = parser.parse_args()

    ctx = build_default_ingestion_context_from_env()
    if ctx is None:
        print(
            "ERROR: Could not build the orchestrator context. Check ClickHouse "
            "/ Postgres / Redis connectivity and that "
            "`instance/integrations/` resolves on disk.",
            file=sys.stderr,
        )
        return 2

    result = run_binding(
        ctx,
        binding_id=args.binding,
        period=args.period,
        triggered_by=args.triggered_by,
        notes=args.notes,
        stop_after=args.stop_after,
    )

    print(f"load_id      : {result.load_id}")
    print(f"status       : {result.status}")
    if result.stopped_after is not None:
        print(f"stopped_after: {result.stopped_after}")
    if result.rows_landed is not None:
        print(f"rows_landed  : {result.rows_landed}")
    print(f"duration_ms  : {result.duration_ms}")
    if result.error:
        print(f"error        : {result.error}", file=sys.stderr)

    # Exit non-zero on any failure status so this script composes with
    # shell pipelines (`&&` / CI gates).
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
