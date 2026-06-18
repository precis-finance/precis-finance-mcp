# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Dev MCP server registration — strip injected ``_scope`` for a real FastMCP.

The shared ``register_*_tools`` factories accept the agent-injected ``_scope``
parameter and register every tool with a plain ``@mcp.tool()``. The agent
graph and the authenticated external ``/mcp`` transport register those
functions **intact** through a duck-typed registry: they inspect the signature
to inject ``_scope`` (``_make_agent_wrapper``) and strip it from the LLM-facing
schema themselves (``derive_llm_facing_signature``).

A *real* FastMCP — the dev-only, unauthenticated MCP servers in
``precis_mcp/server.py`` and ``precis/server.py`` — is the one consumer that
can't take them intact: its schema builder rejects parameters whose names
start with ``_``. ``DevMcpRegistrar`` wraps such a FastMCP so the factories
register against it unchanged, stripping ``_scope`` from the advertised
signature. That transport never enforces scope anyway (no per-user auth), so
``_scope`` defaulting to ``None`` is the correct dev contract.

Keeping the strip here, at the dev-server boundary, is what lets the factories
hand the production registry the real function — registering a stripped shim
there would make the agent wrapper's ``needs_scope`` false and silently
disable dimension-scope enforcement.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

# Injected params whose leading underscore FastMCP's schema builder rejects.
# ``user_id`` is also injected but carries no underscore, so FastMCP accepts it.
_HIDDEN_PARAMS: frozenset[str] = frozenset({"_scope"})


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
