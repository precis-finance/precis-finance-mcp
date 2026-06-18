# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-memory `FastMCP` substitute that captures tool registrations.

Production code registers MCP tools via the decorator pattern:

    @mcp.tool()
    def list_kpis(...):
        ...

The real `mcp.server.fastmcp.FastMCP` wires those tools into the
JSON-RPC surface; tests don't need that wiring — they just need to call
the registered function bodies. This fake captures `(name -> function)`
into a dict the test can introspect.

Standard usage:

    from tests.fakes.mock_mcp import MockMCP
    from precis_mcp.tools.X import register_X_tools

    mock = MockMCP()
    register_X_tools(mock, ref)
    tool = mock.tools["my_tool"]
    result = tool(...)

Co-exists with `tests/fakes/` semantically as a substitute for the FastMCP
SDK (an external system), not as a domain-object factory.
"""

from __future__ import annotations


__all__ = ["MockMCP"]


class MockMCP:
    """Captures tools registered via `@mcp.tool()` into `self.tools`."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator
