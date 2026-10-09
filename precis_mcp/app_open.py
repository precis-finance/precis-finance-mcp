# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-core ASGI entrypoint — the standalone remote MCP server.

The app a standalone open deployment runs — it installs and serves reads with
no Précis platform present, exposing exactly the open remote surface:

- ``/mcp`` — the OAuth2-protected JSON-RPC transport. It authenticates every
  request itself (JWKS verify + RFC 8707 audience + the provisioning gate),
  so no global auth middleware is needed here.
- ``/.well-known/oauth-protected-resource`` — RFC 9728 discovery.
- ``/.well-known/oauth-authorization-server`` + ``/authorize`` ``/register``
  ``/token`` — the RFC 8414 shims for the legacy claude.ai client.
- ``/health`` — liveness; ``/readyz`` — shared dependency readiness.
- ``/api/artifacts/upload`` and ``/api/artifacts/download`` — capability-authorized byte transfer.

It mounts none of the Précis routers (conversations, reports,
workstreams, the LangGraph agent) and does not import the agent application.
Charts, datasets and file export/transfer belong to this common core. The
Précis agent app adds commercial workflows around the same external transport.

Run: ``uvicorn precis_mcp.app_open:app --host 0.0.0.0 --port 8769``.
"""
from __future__ import annotations

import precis_mcp.secrets  # noqa: F401 — resolve *_FILE before any getenv on a secret

import contextlib
import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from precis_mcp.auth_mode import resolve_for_multiuser
from precis_mcp.mcp_external.discovery import router as discovery_router
from precis_mcp.mcp_external.excel_static import router as excel_static_router
from precis_mcp.mcp_external.oauth_proxy import router as oauth_proxy_router
from precis_mcp.mcp_external.server import router as mcp_router

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail fast at startup if PRECIS_AUTH_MODE is misconfigured (e.g. mode=oidc
    # with no OIDC_ISSUER), rather than letting it surface as an opaque 401.
    # Unset → keycloak (the bundled reference) for dev/test tolerance.
    mode = resolve_for_multiuser()
    logger.info("precis-mcp open server starting: auth mode = %s", mode.value)

    # A configured deploy must have a resolvable /mcp audience — otherwise
    # the RFC 8707 check is silently off (any same-issuer token accepted).
    from precis_mcp.oidc import require_mcp_audience_when_configured
    require_mcp_audience_when_configured()

    # Platform-DB connection pool (psycopg3). min_size=0 → no eager connect.
    from precis_mcp.db import open_platform_pool, close_platform_pool
    open_platform_pool()

    from precis_mcp.artifacts.lifecycle import start_artifacts, stop_artifacts
    from precis_mcp.db import close_redis_client
    cleanup = None
    try:
        # Opt-in conformance preflight (gap 5): when PRECIS_AUTH_PREFLIGHT is on,
        # verify the issuer/JWKS/audience are reachable + consistent at boot rather
        # than letting a misconfig surface as an opaque 401. Off by default (it does
        # network I/O — operators enable it for a clear-failure deploy).
        if os.environ.get("PRECIS_AUTH_PREFLIGHT", "").strip().lower() in ("1", "true", "yes"):
            from precis_mcp.oidc import check_token_contract

            problems = check_token_contract()
            if problems:
                raise RuntimeError(
                    "auth preflight failed:\n  - " + "\n  - ".join(problems)
                )
            logger.info("auth preflight OK")
        cleanup = await start_artifacts()
        yield
    finally:
        try:
            if cleanup is not None:
                await stop_artifacts(cleanup)
        finally:
            close_redis_client()
            close_platform_pool()


app = FastAPI(title="Précis Finance MCP", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Liveness — the process is up. No dependency checks (see /readyz)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness — the dependencies the open server needs to serve a request:
    the platform DB (auth + audit) and ClickHouse (the read data plane). Redis
    backs temporary datasets; artifact checks verify schema, signing and local
    storage. Returns 503 until these are available, so an orchestrator holds
    traffic off a starting or degraded instance.

    A sync def so FastAPI runs the blocking driver calls in its threadpool
    rather than on the event loop.
    """
    from precis_mcp.artifacts.lifecycle import readiness_response
    return readiness_response()


# /mcp self-authenticates per request; the discovery + proxy paths are
# anonymous by design. No JWTAuthMiddleware — it exists in the Précis agent app only to gate
# the Précis routers this app does not mount.
app.include_router(mcp_router)
from precis_mcp.artifacts.routes import router as artifact_router, RedactTransferTokens
app.include_router(artifact_router)
app.add_middleware(RedactTransferTokens)
app.include_router(discovery_router)
app.include_router(oauth_proxy_router)
# Excel add-in bundle + host-templated manifest at /excel — self-gates on the
# add-in being enabled and the bundle being present (404 otherwise).
app.include_router(excel_static_router)
