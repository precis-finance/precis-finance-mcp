# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Dev MCP server registration — strip injected ``_scope`` for a real FastMCP.

The shared ``register_*_tools`` factories accept injected ``_scope`` and
``user_id`` parameters and register tools with ``@mcp.tool()``. FastMCP rejects
parameter names starting with ``_``, so ``DevMcpRegistrar`` removes injected
parameters from the advertised signature and supplies ``user_id`` from the
active authentication context.

The single-user server in ``precis_mcp/server.py`` authenticates MCP calls with
a shared dev key and uses a fixed administrator context. It has no per-user
scope restrictions, so ``_scope`` retains its default of ``None``.

Signature stripping belongs only at this boundary. Other registries need the
original signatures to identify and inject scope parameters correctly.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

# Injected params whose leading underscore FastMCP's schema builder rejects.
# ``user_id`` is also injected but carries no underscore, so FastMCP accepts it.
_HIDDEN_PARAMS: frozenset[str] = frozenset({"_scope", "user_id"})


def _strip_hidden(func: Callable) -> Callable:
    """Return a passthrough whose advertised signature omits the hidden params.

    The shim forwards every call verbatim (``**kwargs``); it differs from
    ``func`` only in the ``__signature__`` / ``__annotations__`` FastMCP reads.
    A function with no hidden params is returned unchanged.
    """
    sig = inspect.signature(func)
    hidden = _HIDDEN_PARAMS & set(sig.parameters)
    if not hidden:
        return func

    def shim(**kwargs: Any) -> Any:
        if "user_id" in hidden:
            from precis_mcp.auth import get_auth_context
            kwargs["user_id"] = get_auth_context().user_id
        return func(**kwargs)

    shim.__name__ = func.__name__
    shim.__qualname__ = func.__qualname__
    shim.__doc__ = func.__doc__
    shim.__module__ = func.__module__
    shim.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=[p for n, p in sig.parameters.items() if n not in hidden]
    )
    shim.__annotations__ = {
        k: v
        for k, v in getattr(func, "__annotations__", {}).items()
        if k not in hidden
    }
    return shim


class DevMcpRegistrar:
    """Wrap a real ``FastMCP`` so ``register_*_tools(...)`` can register against
    it with plain ``@mcp.tool()`` despite ``_scope``-bearing signatures.

    Overrides ``.tool()`` to strip hidden params; proxies every other attribute
    straight through to the wrapped FastMCP.
    """

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        register = self._mcp.tool(*args, **kwargs)

        def decorator(func: Callable) -> Callable:
            register(_strip_hidden(func))
            return func

        return decorator

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup misses (i.e. not `tool` / `_mcp`).
        return getattr(self._mcp, name)
