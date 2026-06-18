# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""End-to-end OAuth2 proof for the open MCP connector.

One happy path: obtain a *real* Keycloak access token, then call the live
``/mcp`` endpoint of a running open-package deployment and complete a read.
This is the formal artifact for the split's definition of done:
"the MCP-external connector completes the OAuth2 flow and a read end-to-end
against the open package alone."

Marked `slow` (end-to-end). Skipped by default; auto-skips
unless the live-deployment env is wired, so a developer box just no-ops it.
It runs in the server acceptance environment, where a real Keycloak realm and
a running open-package ``/mcp`` exist.

Token acquisition is the OAuth2 **direct (password) grant** against the seeded
realm client. This deliberately does not drive the
``authorize``-redirect/PKCE leg — that is Keycloak's own well-tested code. What
this proves is the integration-risk half the split actually owns:
  - the realm's protocol mappers emit ``precis_user_id`` and the RFC 8707 MCP
    audience on a real RS256 token;
  - the open verifier (JWKS issuer + audience binding) accepts it;
  - the unprovisioned/provisioned user gate resolves the user;
  - a read tool returns ``structuredContent`` — over the network, against the
    open package with no Précis platform in the loop.

Required env (all must be set or the file no-ops):
  - ``PRECIS_E2E_MCP_URL``        — origin of the running open deployment
                                    (serves ``/mcp`` + the RFC 9728 discovery doc)
  - ``PRECIS_E2E_KC_CLIENT_ID``   — a direct-grant-enabled realm client
  - ``PRECIS_E2E_KC_USERNAME`` / ``PRECIS_E2E_KC_PASSWORD`` — a seeded e2e user
Optional:
  - ``PRECIS_E2E_KC_CLIENT_SECRET`` — when the direct-grant client is confidential
  - ``PRECIS_E2E_READ_TOOL``        — read tool to exercise (default: ``list_scenarios``)
"""

from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# Opt-in gate — skip cleanly if the live deployment isn't wired
# ---------------------------------------------------------------------------


REQUIRED_ENV = (
    "PRECIS_E2E_MCP_URL",
    "PRECIS_E2E_KC_CLIENT_ID",
    "PRECIS_E2E_KC_USERNAME",
    "PRECIS_E2E_KC_PASSWORD",
)


def _env_ready() -> bool:
    return all(os.environ.get(k) for k in REQUIRED_ENV)


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _env_ready(),
        reason=(
            "mcp oauth2 e2e needs a live open deployment + Keycloak; set "
            "PRECIS_E2E_* env vars and run `pytest --slow` from the server "
            "acceptance environment"
        ),
    ),
]


_TIMEOUT = 30.0


def _discover(mcp_url: str) -> tuple[str, str]:
    """RFC 9728 discovery against the live deployment.

    Returns ``(issuer, resource_audience)`` from the open package's own
    ``/.well-known/oauth-protected-resource`` — the same doc a real MCP
    client reads to bootstrap.
    """
    import httpx

    res = httpx.get(
        f"{mcp_url.rstrip('/')}/.well-known/oauth-protected-resource",
        timeout=_TIMEOUT,
    )
    res.raise_for_status()
    body = res.json()
    return body["authorization_servers"][0], body["resource"]


def _token_endpoint(issuer: str) -> str:
    """Read the IdP's OIDC discovery to find its token endpoint."""
    import httpx

    res = httpx.get(
        f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=_TIMEOUT
    )
    res.raise_for_status()
    return res.json()["token_endpoint"]


def _direct_grant(token_endpoint: str) -> dict:
    """OAuth2 password grant against the seeded realm client → real token set."""
    import httpx

    form = {
        "grant_type": "password",
        "client_id": os.environ["PRECIS_E2E_KC_CLIENT_ID"],
        "username": os.environ["PRECIS_E2E_KC_USERNAME"],
        "password": os.environ["PRECIS_E2E_KC_PASSWORD"],
        "scope": "openid",
    }
    secret = os.environ.get("PRECIS_E2E_KC_CLIENT_SECRET")
    if secret:
        form["client_secret"] = secret
    res = httpx.post(token_endpoint, data=form, timeout=_TIMEOUT)
    res.raise_for_status()
    return res.json()


def _rpc(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_mcp_oauth2_flow_completes_a_read_end_to_end():
    """Real Keycloak token → live /mcp → a read returns structured content."""
    import jwt as pyjwt
    import httpx

    mcp_url = os.environ["PRECIS_E2E_MCP_URL"].rstrip("/")
    read_tool = os.environ.get("PRECIS_E2E_READ_TOOL", "list_scenarios")

    # 1. RFC 9728 discovery off the open package, then OIDC discovery off the IdP.
    issuer, resource = _discover(mcp_url)
    token_endpoint = _token_endpoint(issuer)

    # 2. Obtain a real RS256 access token (direct grant).
    tokens = _direct_grant(token_endpoint)
    access_token = tokens["access_token"]

    # 3. The realm mappers must stamp the claims the open verifier + audience
    #    gate depend on — this is the realm-config assertion the e2e exists for.
    claims = pyjwt.decode(access_token, options={"verify_signature": False})
    assert claims.get("precis_user_id") or claims.get("preferred_username"), (
        "realm must emit precis_user_id (or preferred_username) on the MCP token"
    )
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    assert resource in aud_list, (
        f"token aud {aud_list} must include the MCP resource {resource!r} "
        "(RFC 8707 audience mapper missing on the direct-grant client)"
    )

    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(base_url=mcp_url, timeout=_TIMEOUT, headers=headers) as c:
        # 4. initialize handshake.
        init = c.post("/mcp", json=_rpc("initialize", {"capabilities": {}}))
        assert init.status_code == 200, init.text
        assert "result" in init.json()

        # 5. tools/list — the read tool must be advertised on the open surface.
        listed = c.post("/mcp", json=_rpc("tools/list", {}, rpc_id=2))
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert read_tool in names, f"{read_tool} not in advertised tools {names}"

        # 6. tools/call — a read returns without error.
        called = c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": read_tool, "arguments": {}}, rpc_id=3),
        )
        assert called.status_code == 200, called.text
        result = called.json()["result"]
        assert not result.get("isError"), result
        assert "structuredContent" in result or result.get("content"), result
