# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Public MCP transport — `/mcp` route group.

Second transport above the shared tool surface (`process_tool_call` chokepoint).
See `reference/mcp-tools.md` for the framing rules.
"""
from precis_mcp.mcp_external.server import router  # re-exported for app wiring

__all__ = ["router"]
