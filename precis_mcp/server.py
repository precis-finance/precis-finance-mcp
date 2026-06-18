# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-core single-user dev server — the no-Keycloak local trial.

The "instant local" on-ramp for the open package: a FastMCP Streamable HTTP
server exposing ONLY the open read surface (metric/statement/inspect/list/
search, ingestion reads) behind a single shared dev key. No OAuth, no Keycloak
— the dominant open-source adoption path. For a multi-user remote
server use the OAuth2 transport in ``app_open`` instead.

This is the open counterpart to the Précis dev server: same dev-key
gate + Streamable HTTP scaffolding, but it registers no write/plan/report/chart/
excel/sandbox tools and imports nothing from the ``precis`` package.

Run via the bundle (deploy/docker-compose.local.yml) or directly:
    ENABLE_MCP_DEV_SERVER=1 MCP_DEV_KEY=$(openssl rand -hex 32) \
      python -m precis_mcp.server
Every request must carry ``Authorization: Bearer <MCP_DEV_KEY>``.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import precis_mcp.secrets  # noqa: E402,F401 — resolve *_FILE before any getenv on a secret

from mcp.server.fastmcp import FastMCP  # noqa: E402

from precis_mcp.catalogue_ref import _catalogue_ref as catalogue_ref  # noqa: E402
from precis_mcp.mcp_external.instructions import build_mcp_instructions  # noqa: E402

mcp = FastMCP(
    "Précis-MCP (open)",
    # No host/port here: the dev SSE server binds via uvicorn.run(host=_bind_host,
    # port=_bind_port) below, which defaults to 127.0.0.1. A constructor host here
    # is dead but reads as a 0.0.0.0 exposure to an auditor — keep it absent.
    instructions=build_mcp_instructions(catalogue_ref.current),
)

# Open read surface only. No Précis platform tools are imported or registered.
from precis_mcp.tools.read_tools import register_read_tools  # noqa: E402
from precis_mcp.tools._dev_mcp import DevMcpRegistrar  # noqa: E402

# A real FastMCP rejects the agent-injected `_scope` param (leading underscore);
# DevMcpRegistrar strips it at this dev-server boundary. The agent/external
# transports register the raw functions and inject `_scope` themselves.
register_read_tools(DevMcpRegistrar(mcp), catalogue_ref)

# Ingestion subsystem (open): registers the ingestion read tools + builds the
# IntegrationRegistryRef the engine resolves federated reads through. Shared with
# the engine's Ibis registry so federated sources and hot reloads line up — the
# same wiring the Précis dev server uses.
from precis_mcp.ingestion.wiring import attach_ingestion_to_mcp  # noqa: E402
from precis_mcp.engine.ibis_registry import set_integration_registry  # noqa: E402

integration_ref = attach_ingestion_to_mcp(mcp)
set_integration_registry(integration_ref)


if __name__ == "__main__":
    # The transport here has no per-user auth and is for local single-user
    # use only. Gates protect against accidental exposure:
    #   0. PRECIS_AUTH_MODE must select devkey (unset → devkey); a multi-user
    #      mode (keycloak/oidc) refuses — wrong entrypoint, use app_open.
    #   1. ENABLE_MCP_DEV_SERVER=1 must be explicit — no default-on.
    #   2. MCP_DEV_KEY (>=32 chars) must be set; every request must carry it
    #      as `Authorization: Bearer <key>`.
    #   3. Binds to 127.0.0.1 by default; override with MCP_BIND_HOST only when
    #      a remote tunnel actually needs it.
    import hmac

    from precis_mcp.auth_mode import AuthModeError, resolve_for_devkey

    try:
        resolve_for_devkey()
    except AuthModeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if os.getenv("ENABLE_MCP_DEV_SERVER") != "1":
        print(
            "MCP dev server disabled. Set ENABLE_MCP_DEV_SERVER=1 to enable. "
            "This server has no per-user auth and must never be exposed in "
            "production — use the OAuth2 transport (precis_mcp.app_open) there.",
            file=sys.stderr,
        )
        sys.exit(0)

    _dev_key = (os.getenv("MCP_DEV_KEY") or "").strip()
    if not _dev_key:
        print(
            "MCP_DEV_KEY must be set when ENABLE_MCP_DEV_SERVER=1. Generate one "
            "with `openssl rand -hex 32` and pass it as "
            "`Authorization: Bearer <key>` on every request.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(_dev_key) < 32:
        print("MCP_DEV_KEY must be at least 32 characters of entropy.", file=sys.stderr)
        sys.exit(1)

    _bind_host = os.getenv("MCP_BIND_HOST", "127.0.0.1")
    _bind_port = int(os.getenv("MCP_BIND_PORT", "8768"))

    # Single-user server: the dev key is the authentication; the fixed admin
    # context is the authorisation the tool layer reads (get_auth_context()).
    # Set before the event loop starts so every task inherits the contextvar.
    from precis_mcp.auth import AuthContext, UserPermissions, set_auth_context

    set_auth_context(AuthContext(
        user_id="dev",
        permissions=UserPermissions(user_id="dev", is_admin=True),
    ))

    import uvicorn
    from starlette.responses import JSONResponse

    # Pure-ASGI bearer gate. NOT BaseHTTPMiddleware: that bridges the downstream
    # app through a child task + memory stream, which unwinds badly when an SSE
    # response is cancelled by a client disconnect (spurious cancel-scope /
    # "expected http.response.body, got http.disconnect" tracebacks). A header
    # check only needs to inspect the scope and either short-circuit with a 401
    # or pass scope/receive/send through untouched, so the SSE stream is never
    # wrapped. See Starlette encode/starlette#1438.
    class _DevKeyAuth:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                resp = JSONResponse({"detail": "missing bearer token"}, status_code=401)
                return await resp(scope, receive, send)
            if not hmac.compare_digest(auth[len("Bearer "):], _dev_key):
                resp = JSONResponse({"detail": "invalid bearer token"}, status_code=401)
                return await resp(scope, receive, send)
            await self.app(scope, receive, send)

    # Streamable HTTP transport on /mcp. The returned app carries FastMCP's
    # session-manager lifespan, so running it directly starts the manager.
    _app = mcp.streamable_http_app()
    _app.add_middleware(_DevKeyAuth)
    uvicorn.run(_app, host=_bind_host, port=_bind_port)
