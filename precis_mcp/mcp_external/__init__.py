# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Public MCP transport — `/mcp` route group.

Second transport above the shared tool surface (`process_tool_call` chokepoint).
See `docs/archive_specs/mcp_external_surface_spec.md` for the framing rules and the
sequenced phasing.
"""
from precis_mcp.mcp_external.server import router  # re-exported for app wiring

__all__ = ["router"]
