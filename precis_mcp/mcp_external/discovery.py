# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""RFC 9728 OAuth-protected-resource discovery.

Advertises that the `/mcp` resource server delegates token issuance to the
Keycloak `precis` realm.  External MCP clients (claude.ai, ChatGPT) hit
``/.well-known/oauth-protected-resource`` to discover the AS endpoint and
PKCE/DCR configuration; from there they negotiate directly against Keycloak.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from precis_mcp import oidc


router = APIRouter()


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> dict:
    """Point external MCP hosts at the Keycloak realm's OIDC discovery."""
    base = oidc.public_base(str(request.base_url))
    resource = oidc.mcp_audience() or f"{base}/mcp"
    return {
        "resource": resource,
        "authorization_servers": [oidc.config.issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://precis.finance/docs/mcp",
    }
