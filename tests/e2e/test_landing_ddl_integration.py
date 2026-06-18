# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""End-to-end tests for landing DDL against real PostgreSQL / ClickHouse.

Currently empty: the source ``tests/test_landing_ddl.py`` file contains
only pure-logic tests (DDL string rendering, drift detection against an
injected live-schema callback, CLI smoke test writing to ``tmp_path``).
None of them open a real database connection, so all 21 source tests
moved to ``tests/unit/test_landing_ddl.py``.

This file is reserved for future e2e coverage — e.g. applying generated
DDL to a real ClickHouse instance and validating round-trip schema
introspection, or running the CLI against a populated registry and
asserting on materialised tables. Every test added here must hit a real
service and be marked ``@pytest.mark.slow``.
"""

from __future__ import annotations
