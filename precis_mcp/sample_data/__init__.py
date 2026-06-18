# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Synthetic sample data — the populated demo model for evaluation.

`python -m precis_mcp.sample_data` generates the dataset, lands it in the
mock customer Postgres, runs every ingestion binding into ClickHouse, and
seeds the plan scenarios into `live.fact_plan`.

Import `precis_mcp.sample_data.generate` directly (not re-exported here:
the module seeds RNGs and reads .env at import time, which package
importers should opt into explicitly).
"""
