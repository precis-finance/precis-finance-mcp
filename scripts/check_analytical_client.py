#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
"""Read-only native Databricks transport check, not integrated engine acceptance.

Use existing resolved environment credentials; never prints tokens, parameters
or financial source rows. No DDL, DML, temporary tables or warehouse settings
are changed. A query can automatically start a stopped SQL warehouse.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from importlib.metadata import version
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from precis_mcp.analytical import get_analytical_client
from precis_mcp.analytical.config import quote_identifier
from precis_mcp.analytical.contracts import AnalyticalError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", default="semantic")
    parser.add_argument("--view", default="v_pnl")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence path; contains no credentials or source rows.")
    args = parser.parse_args()
    started = perf_counter()
    try:
        view = ".".join(quote_identifier(x) for x in (args.catalog, args.schema, args.view))
        with get_analytical_client("databricks") as client:
            amount = Decimal("12345678901234567890.1234")
            stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            result = client.query(
                "SELECT CAST(:amount AS DECIMAL(38,4)) AS amount, "
                "CAST(:missing AS DECIMAL(18,4)) AS missing, "
                ":label AS label, :day AS day, :stamp AS stamp",
                {"amount": amount, "missing": None, "label": "literal ' ; -- :amount",
                 "day": date(2024, 2, 29), "stamp": stamp},
            )
            row = result.result_rows[0]
            assert row[0] == amount and isinstance(row[0], Decimal), "Decimal round-trip failed"
            assert row[1] is None and row[2] == "literal ' ; -- :amount", "Null/string binding failed"
            assert row[3] == date(2024, 2, 29), "Date binding failed"
            # The connector may return a naive timestamp; its session is UTC.
            normalized_stamp = row[4].replace(tzinfo=timezone.utc) if row[4].tzinfo is None else row[4]
            assert normalized_stamp == stamp, "Timestamp instant changed"
            assert len(result.column_types) == 5, "Missing native result types"
            count = client.query(f"SELECT COUNT(*) AS row_count FROM {view}")
            sample = client.query(f"SELECT * FROM {view} LIMIT 5")
            assert count.result_rows[0][0] > 0 and sample.result_rows, "Semantic view is empty"
        summary = {
            "status": "passed", "checks": ["native_parameters", "decimal", "null", "string", "date",
                                            "utc_timestamp", "semantic_view", "session_reuse_and_close"],
            "view_row_count": count.result_rows[0][0], "sample_rows": len(sample.result_rows),
            "column_names": sample.column_names, "column_types": sample.column_types,
            "elapsed_seconds": round(perf_counter()-started, 3),
            "evidence_scope": "native client only; not reporting-engine parity or a latency benchmark",
            "versions": {name: version(name) for name in ("databricks-sql-connector", "databricks-sdk", "oauthlib")},
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(summary, indent=2)+"\n")
        print(json.dumps(summary))
        return 0
    except (AnalyticalError, AssertionError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
