# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Server `instructions` for the external MCP transport.

The MCP `initialize` result carries an optional `instructions` string the host
SHOULD fold into the model's context. It is the closest thing MCP offers to
"publishing a skill": the orientation, data model, and cross-tool flows the Précis
agent gets from its skill prompts, condensed for the read-only MCP surface.

Composed from a trimmed template (`mcp_external.md`, co-located) plus the live
catalogue's data-model sections — the SAME renderers Précis's system prompt uses
(`render_statements_table` / `render_scenarios_table` / `render_dimensions_table`),
so the data model always matches what this instance actually serves. It is
**static across users** (no profile, scope, or report context is injected) and
**dynamic per instance** (the catalogue). Composed per `initialize` (infrequent,
once per session) so it always reflects the current catalogue — no cache to
invalidate on reload.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "mcp_external.md"

def build_mcp_instructions(catalogue, scenario_registry=None) -> str:
    """Compose the instructions block: the active template with `{company_name}`
    and the catalogue's data-model sections injected. `catalogue=None` (dev with
    no catalogue) drops the data-model sections rather than failing."""
    from precis_mcp.catalogue_sections import (
        render_dimensions_table,
        render_scenarios_table,
        render_statements_table,
    )

    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    from precis_mcp.artifacts.policy import external_uploads_enabled
    upload_guidance = (
        "To receive a file, call `begin_file_upload(filename, mime_type, size_bytes)`, "
        "send the exact raw bytes to its `upload_url` using HTTP PUT, then call "
        "`complete_file_upload(upload_id)`. Completion is idempotent. This registers "
        "a file; it does not ingest or mutate financial data. An agent host must "
        "support HTTP transfer from its sandbox or provide an attachment adapter; "
        "an MCP resource link alone does not copy bytes into that sandbox."
        if external_uploads_enabled()
        else "Receiving files is disabled on this connector. Exports and downloads remain available."
    )
    text = text.replace("{upload_guidance}", upload_guidance)
    company = (os.environ.get("COMPANY_NAME") or "").strip() or "your organisation"
    text = text.replace("{company_name}", company)

    if catalogue is not None:
        text = text.replace("{statements}", render_statements_table(catalogue))
        text = text.replace(
            "{scenarios}",
            render_scenarios_table(catalogue, scenario_registry=scenario_registry),
        )
        text = text.replace("{dimensions}", render_dimensions_table(catalogue))
    else:
        for placeholder in ("{statements}", "{scenarios}", "{dimensions}"):
            text = text.replace(placeholder, "")
    return text


def get_mcp_instructions() -> str:
    """The instructions for this process, composed from the live catalogue ref.

    Best-effort: any failure (missing template, renderer error) logs and returns
    an empty string — the connector still works, just without the skill layer."""
    try:
        from precis_mcp.catalogue_ref import _catalogue_ref
        catalogue = _catalogue_ref.current
    except Exception:
        catalogue = None
    # Scenarios render from the live registry (semantic.scenarios), loaded the
    # same way the gate does. Best-effort — None renders a placeholder line.
    scenario_registry = None
    try:
        from precis_mcp.db import get_clickhouse_client
        from precis_mcp.engine.scenario_registry import load_scenario_registry
        scenario_registry = load_scenario_registry(get_clickhouse_client())
    except Exception:
        logger.warning("MCP instructions: scenario registry unavailable", exc_info=True)
    try:
        return build_mcp_instructions(catalogue, scenario_registry)
    except Exception:
        logger.exception("Failed to build MCP instructions — serving none")
        return ""
