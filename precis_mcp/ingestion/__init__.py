# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Ingestion subsystem — Ibis-driven pipeline.

Reads `instance/integrations/{sources,bindings}/*.yml` configs and
drives the three-stage pipeline:

    extract  →  validate  →  swap

Bindings declare an operator-authored SQL query run via Ibis on the
source warehouse. Aggregation pushes down to the source. Result rows
stream into `staging.<table>`, get shape-validated, and atomically
promote into `live.<table>` via REPLACE PARTITION (period bindings) or
EXCHANGE TABLES (snapshot bindings).
"""