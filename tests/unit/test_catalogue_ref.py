# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the cold-start catalogue-load surfacing in `catalogue_ref`.

A bad instance catalogue must fail the process fast — but with a specific,
actionable log line, not an opaque import crash-loop. `_load_initial_catalogue`
takes an injectable loader so the surfacing logic is testable without a real
catalogue on disk.
"""

from __future__ import annotations

import logging

import pytest

from precis_mcp.catalogue_ref import _load_initial_catalogue
from precis_mcp.engine import CatalogueError


def test_passes_through_on_success():
    assert _load_initial_catalogue("dir", loader=lambda d: "CATALOGUE") == "CATALOGUE"


def test_logs_specific_reason_then_reraises(caplog):
    def boom(_d):
        raise CatalogueError("duplicate metric 'revenue' in pnl.yml")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(CatalogueError):
            _load_initial_catalogue("dir", loader=boom)

    # The failure is surfaced: a specific, actionable line names the reason.
    assert "duplicate metric 'revenue' in pnl.yml" in caplog.text
    assert "refusing to serve" in caplog.text
