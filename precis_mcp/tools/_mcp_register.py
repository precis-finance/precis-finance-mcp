# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""FastMCP registration shim for tools with agent-injected parameters.

Some tools (``run_statement``, ``run_metric``, write/lock tools) accept a
``_scope`` parameter that the agent wrapper injects from the ``_call_scope``
contextvar. FastMCP's schema builder rejects parameters whose names start
with ``_``, so those tools cannot be registered with a plain ``@mcp.tool()``.

``register_mcp_tool`` wraps ``@mcp.tool()`` with a shim that strips hidden
parameters from the FastMCP-exposed signature. The MCP path (dev-only,
unauthenticated) invokes the shim, which calls the underlying function and
lets the hidden params take their defaults (``_scope=None`` → no enforcement,
matching the no-auth contract). The agent path is unaffected — it calls the
underlying function directly through its own wrapper, which still reads the
full signature.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

_HIDDEN_PARAMS: frozenset[str] = frozenset({"_scope"})


def register_mcp_tool(mcp: FastMCP) -> Callable[[Callable], Callable]:
    """Decorator: register ``func`` with FastMCP, stripping hidden params."""

    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)
        hidden = _HIDDEN_PARAMS & set(sig.parameters)
        if not hidden:
            mcp.tool()(func)
            return func

        visible_params = [
            p for name, p in sig.parameters.items() if name not in hidden
        ]
        visible_sig = sig.replace(parameters=visible_params)

        def shim(**kwargs: Any) -> Any:
            return func(**kwargs)

        shim.__name__ = func.__name__
        shim.__qualname__ = func.__qualname__
        shim.__doc__ = func.__doc__
        shim.__module__ = func.__module__
        shim.__signature__ = visible_sig  # type: ignore[attr-defined]
        shim.__annotations__ = {
            k: v
            for k, v in getattr(func, "__annotations__", {}).items()
            if k not in hidden
        }

        mcp.tool()(shim)
        return func

    return decorator