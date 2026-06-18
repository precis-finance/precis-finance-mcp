# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Live catalogue reference — process-global holder for the validated catalogue.

Open-core module. Both transports (the LangGraph agent and the external MCP
server) read the live catalogue through the shared ``_catalogue_ref`` singleton,
so a reload is visible to both. Kept out of the Précis agent application
so the open MCP path can reach the catalogue without importing it.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Protocol

from precis_mcp.engine import CatalogueError, load_and_validate

if TYPE_CHECKING:
    from precis_mcp.engine.catalogue import Catalogue

logger = logging.getLogger(__name__)


def _load_initial_catalogue(catalogue_dir, loader=load_and_validate):
    """Load the catalogue at cold start, surfacing a validation failure as a
    specific, actionable log line before re-raising.

    A bad instance catalogue should fail the process fast — there is nothing
    valid to serve — but the failure must be legible: a one-line reason at the
    top of the logs naming the offending entry, not an opaque import traceback
    that reads as a generic crash-loop. Re-raising preserves
    fail-fast; the log line is the surfacing.
    """
    try:
        return loader(catalogue_dir)
    except CatalogueError as exc:
        logger.error(
            "Catalogue failed to validate at startup — refusing to serve. "
            "Fix the instance catalogue and restart. Reason: %s",
            exc,
        )
        raise


class CatalogueRef(Protocol):
    """Structural type for a live-catalogue holder — anything exposing
    ``.current`` (the validated catalogue). Both the open ``_CatalogueRef``
    singleton below and the dev server's concrete ``CatalogueRef`` satisfy it.
    Tools annotate their ``ref`` parameter against this, so the open tool
    modules don't reference the Précis server's concrete type."""

    current: "Catalogue"

CATALOGUE_DIR = os.path.join(os.path.dirname(__file__), "..", "instance", "catalogue")
_catalogue = _load_initial_catalogue(CATALOGUE_DIR)


class _CatalogueRef:
    def __init__(self):
        self.current = _catalogue

    def reload(self):
        self.current = load_and_validate(CATALOGUE_DIR)
        from precis_mcp.engine import check_dimension_sources
        warnings = check_dimension_sources(self.current)
        if warnings:
            logger.warning(
                "Catalogue reloaded with %d dimension source warning(s): %s",
                len(warnings), "; ".join(warnings),
            )
        return "ok"


_catalogue_ref = _CatalogueRef()
