# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Server `instructions` for the external MCP transport.

The MCP `initialize` result carries an optional `instructions` string the host
SHOULD fold into the model's context. It is the closest thing MCP offers to
"publishing a skill": the orientation, data model, and cross-tool flows the SPA
agent gets from its skill prompts, condensed for the read-only MCP surface.

Composed from a trimmed template (`mcp_external.md`, co-located) plus the live
catalogue's data-model sections — the SAME renderers the SPA system prompt uses
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

# Commercial override. The open package ships the read-only template
# (`mcp_external.md`, co-located) describing only the open surface — statements,
# metrics, the `_data` variants, and inspection. The commercial product
# advertises more tools (charts via `eval_chart_transform`, Excel download
# variants) and registers its own *full* template here at startup, which
# replaces the open one wholesale (same `{company_name}` / `{statements}` /
# `{scenarios}` / `{dimensions}` placeholders). An open deployment registers
# none and serves the co-located open template. Two-prompt overwrite, not a
# fragment merge — the commercial surface diverges enough that a full
# replacement is cleaner than stitching.
_TEMPLATE_OVERRIDE: str | None = None


def register_instructions_template(text: str) -> None:
    """Commercial seam: replace the open instructions template wholesale.

    Idempotent; last registration wins. Called once at commercial startup
    (alongside the commercial tool-loader registration)."""
    global _TEMPLATE_OVERRIDE
    _TEMPLATE_OVERRIDE = text


def _template_text() -> str:
    """The active template body — the commercial override if registered, else
    the co-located open read-only template."""
    if _TEMPLATE_OVERRIDE is not None:
        return _TEMPLATE_OVERRIDE
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def build_mcp_instructions(catalogue, scenario_registry=None) -> str:
    """Compose the instructions block: the active template with `{company_name}`
    and the catalogue's data-model sections injected. `catalogue=None` (dev with
    no catalogue) drops the data-model sections rather than failing."""
    from precis_mcp.catalogue_sections import (
        render_dimensions_table,
        render_scenarios_table,
        render_statements_table,
    )

    text = _template_text()
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
