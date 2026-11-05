#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
"""Render Précis-owned Databricks read objects for operator review, without I/O to a database.

Customer actuals and master-data views remain customer-owned. This creates a
local JSON bundle of qualified SQL and bound seed parameters; it does not apply
DDL, migrate data, overwrite scenario governance or require warehouse secrets.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from precis_mcp.analytical.provisioning import hierarchy_provisioning, read_provisioning
from precis_mcp.analytical.sql import SqlDialect
from precis_mcp.engine import load_and_validate
from precis_mcp.ingestion.scenario_runner import parse_scenarios


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--semantic-schema", default="semantic")
    parser.add_argument("--actuals-schema", default="actuals")
    parser.add_argument("--planning-schema", default="precis_planning")
    parser.add_argument("--catalogue", default="instance/catalogue")
    parser.add_argument("--scenarios", default="instance/scenarios.yml")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hierarchy-refresh-only", action="store_true",
                        help="Render only hierarchy table replacements for the customer pipeline; no scenario seed file is needed.")
    args = parser.parse_args()
    dialect = SqlDialect("databricks", args.catalog, (
        ("semantic", args.semantic_schema), ("planning", args.planning_schema),
        ("live", args.actuals_schema), ("actuals", args.actuals_schema)))
    catalogue = load_and_validate(args.catalogue)
    if args.hierarchy_refresh_only:
        statements = hierarchy_provisioning(catalogue, dialect, refresh_only=True)
    else:
        seeds = parse_scenarios(Path(args.scenarios))
        statements = read_provisioning(catalogue, seeds, dialect)
    bundle = {"version":1, "backend":"databricks", "statements":[asdict(statement) for statement in statements],
              "operator_contract":"Review before applying with provisioning credentials. Serialize with all scenario writers; "
                  "validate existing and proposed aliases/IDs before seed MERGE. Existing scenario rows are never updated. "
                  "No uniqueness guarantee is inferred from MERGE. Validate required inputs read-only before starting the app."}
    hierarchy_contract = (
        "Initial provisioning creates backing tables and stable rollup views. After hierarchy inputs change, "
        "refresh from the customer pipeline with operator credentials; never on a timer or from read tools. "
        "Serialize hierarchy loads and keep affected reporting unavailable until all replacements succeed. "
        "Replacement is atomic per table, not across sources and hierarchy mappings. On failure, earlier "
        "replacements or source updates may already be visible; reconcile before resuming reporting. "
        "Rerun full provisioning after hierarchy definition changes.")
    bundle["operator_contract"] = hierarchy_contract if args.hierarchy_refresh_only else bundle["operator_contract"] + " " + hierarchy_contract
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, indent=2, default=str)+"\n")
    print(f"Rendered {len(statements)} statements; no warehouse calls or mutations performed.")


if __name__ == "__main__":
    main()
