# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Request identity and audience validation shared by MCP and artifact routes."""
from __future__ import annotations
import logging
from fastapi import Request
from precis_mcp import oidc
from precis_mcp.auth import AuthContext, AccountDisabledError, AuthError, load_permissions, resolve_user_id
logger = logging.getLogger(__name__)

class _AuthRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail


def _check_mcp_audience(claims: dict) -> None:
    """RFC 8707 audience binding.

    Enforced whenever an audience is resolvable (`PRECIS_BASE_URL` set, or
    an explicit `KC_MCP_AUDIENCE`); see `oidc.mcp_audience`.  Off only on a
    dev box with no base URL configured.
    """
    expected = oidc.mcp_audience()
    if not expected:
        return
    aud = claims.get("aud")
    aud_list = [aud] if isinstance(aud, str) else (aud or [])
    if expected not in aud_list:
        # Don't fail silently. A token that verified (valid signature + issuer)
        # but lacks our /mcp audience almost always means Keycloak is stamping a
        # stale audience after a PRECIS_BASE_URL change, or a self-registered
        # (DCR) client — e.g. claude.ai — never received the precis-mcp scope.
        # Surface the actionable cause; reads only the token's own claims, so no
        # admin credential is involved.
        logger.warning(
            "MCP audience mismatch: token aud=%s, expected %r. If PRECIS_BASE_URL "
            "changed or a new connector self-registered, re-run the Keycloak realm "
            "reconcile (the keycloak-realm-apply one-shot) to restamp the audience "
            "mapper and re-promote the precis-mcp default client scope.",
            aud_list, expected,
        )
        raise _AuthRejected(
            401, f"Token audience does not include {expected!r}"
        )


def _authenticate(request: Request) -> AuthContext:
    token = oidc.extract_session_token(request)
    if not token:
        raise _AuthRejected(401, "Missing Authorization header")
    try:
        claims = oidc.verify_keycloak_token(token)
    except Exception as exc:
        logger.info("MCP token verification failed: %s", exc)
        raise _AuthRejected(401, "Invalid token")
    _check_mcp_audience(claims)
    # Resolve the platform user_id from the token (default: precis_user_id, with
    # a preferred_username fallback for DCR clients; configurable per gap 2 via
    # PRECIS_IDENTITY_CLAIM / PRECIS_IDENTITY_COLUMN).
    user_id = resolve_user_id(claims)
    if not user_id:
        raise _AuthRejected(403, "Token identity claim missing or unmatched")
    try:
        permissions = load_permissions(user_id)
    except AccountDisabledError as exc:
        raise _AuthRejected(403, str(exc))
    except AuthError:
        raise _AuthRejected(403, f"User {user_id!r} not provisioned")
    return AuthContext(user_id=user_id, permissions=permissions)
