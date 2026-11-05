#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
"""Check configured Databricks inputs and stored Précis scenarios without mutations.

Uses the same validator as application startup. This may start the warehouse
and scan inputs for forbidden nulls; it does not ingest, provision or repair.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from precis_mcp.analytical.runtime import required_read_columns, validate_runtime_inputs
from precis_mcp.engine import load_and_validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", default="instance/catalogue")
    args = parser.parse_args()
    os.environ["PRECIS_ANALYTICAL_BACKEND"] = "databricks"
    catalogue = load_and_validate(args.catalogue)
    validate_runtime_inputs(catalogue)
    print(json.dumps({"status":"passed", "backend":"databricks",
                      "catalogue_relations":len(required_read_columns(catalogue)),
                      "stored_scenarios":"validated", "mutations":False}))


if __name__ == "__main__":
    main()
