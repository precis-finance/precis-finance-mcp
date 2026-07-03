# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""OIDC client logic — Keycloak authorization-code flow + token validation.

Single boundary between Précis and Keycloak.  The middleware reads tokens via
``verify_keycloak_token``; the auth routes call ``build_authorize_url`` +
``exchange_code`` to drive the OIDC dance.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets as pysecrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import Request, Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeycloakConfig:
    """Resolved OIDC settings.  Built once from environment at import.

    Defaults derive the issuer/JWKS from the bundled-Keycloak URL shape
    (modes A/B).  For mode C (direct external OIDC trust) the operator sets
    ``OIDC_ISSUER`` / ``OIDC_JWKS_URL`` to point the verifier at any compliant
    IdP — those explicit overrides win, with the Keycloak-derived path as the
    fallback.  The token/authorize/logout endpoints below stay Keycloak-flow
    specific (used only by the Précis app login); the open verification
    path needs only ``issuer`` + ``jwks_url`` + the audience.
    """
    base_url_internal: str  # backend ↔ Keycloak (e.g. http://localhost:8080/auth)
    base_url_public: str    # browser-facing + issuer claim (e.g. http://localhost/auth)
    realm: str
    client_id: str
    redirect_uri: str       # browser hits this after Keycloak auth — must be public
    issuer_override: str | None = None    # OIDC_ISSUER — mode C
    jwks_url_override: str | None = None  # OIDC_JWKS_URL — mode C
    client_secret: str | None = None      # OIDC_CLIENT_SECRET — confidential pre-registered client (mode C)

    @property
    def issuer(self) -> str:
        # Verbatim override (no rstrip): some IdPs' iss legitimately ends in
        # "/" (e.g. Auth0), and the iss claim must match exactly.
        if self.issuer_override:
            return self.issuer_override
        return f"{self.base_url_public}/realms/{self.realm}"

    @property
    def jwks_url(self) -> str:
        if self.jwks_url_override:
            return self.jwks_url_override
        return f"{self.base_url_internal}/realms/{self.realm}/protocol/openid-connect/certs"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url_internal}/realms/{self.realm}/protocol/openid-connect/token"

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.base_url_public}/realms/{self.realm}/protocol/openid-connect/auth"

    @property
    def logout_endpoint(self) -> str:
        # Back-channel logout — backend posts the refresh token, Keycloak
        # invalidates the SSO session.  Internal URL (server-to-server).
        return f"{self.base_url_internal}/realms/{self.realm}/protocol/openid-connect/logout"


def _config_from_env() -> KeycloakConfig:
    # One deployment-host knob: PRECIS_BASE_URL (the public origin, e.g.
    # https://demo.precis.finance).  The public-facing URLs are derived from
    # it so a new deployment sets one value, not five.  Each derived var is
    # still overridable by its explicit env var (explicit wins) for edge
    # cases.  KC_BASE_URL_INTERNAL is container topology, not host-derived.
    base = os.environ.get("PRECIS_BASE_URL", "").rstrip("/")

    def _derive(var: str, suffix: str, dev_default: str) -> str:
        explicit = os.environ.get(var)
        if explicit:
            return explicit.rstrip("/")
        return f"{base}{suffix}" if base else dev_default

    return KeycloakConfig(
        base_url_internal=os.environ.get(
            "KC_BASE_URL_INTERNAL", "http://localhost:8080/auth"
        ).rstrip("/"),
        base_url_public=_derive("KC_BASE_URL_PUBLIC", "/auth", "http://localhost/auth"),
        realm=os.environ.get("KC_REALM", "precis"),
        # The pre-registered client. OIDC_CLIENT_ID names the client the
        # deployment registered at its IdP (mode C, no DCR); KC_CLIENT_ID / the
        # default cover bundled Keycloak (mode B).
        client_id=(
            os.environ.get("OIDC_CLIENT_ID")
            or os.environ.get("KC_CLIENT_ID", "precis-spa")
        ),
        client_secret=os.environ.get("OIDC_CLIENT_SECRET") or None,
        redirect_uri=_derive(
            "KC_REDIRECT_URI", "/api/auth/callback",
            "http://localhost/api/auth/callback",
        ),
        # Mode C: point the verifier at an external OIDC issuer.  Verbatim —
        # no rstrip (Auth0's iss ends in "/").  Empty/unset → Keycloak-derived.
        issuer_override=os.environ.get("OIDC_ISSUER") or None,
        jwks_url_override=os.environ.get("OIDC_JWKS_URL") or None,
    )


config = _config_from_env()


def public_base(request_base_url: str = "") -> str:
    """The deployment's public origin (no trailing slash).

    ``PRECIS_BASE_URL`` when set, else the caller's request base URL (dev).
    Used to build absolute URLs (e.g. the RFC 9728 metadata URL) with the
    correct scheme: the app sits behind a TLS-terminating proxy, so
    ``request.base_url`` reports ``http`` and can't be trusted for a
    public ``https`` URL.
    """
    base = os.environ.get("PRECIS_BASE_URL", "").rstrip("/")
    return base or request_base_url.rstrip("/")


def mcp_audience() -> str | None:
    """RFC 8707 audience the ``/mcp`` surface requires in a token's ``aud``.

    Resolution order: ``OIDC_AUDIENCE`` (mode C — the de-branded name, set to
    whatever the external IdP stamps as the resource audience; used verbatim so
    it matches the token exactly) → legacy ``KC_MCP_AUDIENCE`` → derived
    ``$PRECIS_BASE_URL/mcp``.  ``None`` when none is set — audience enforcement
    is then off (dev with no base URL).
    """
    oidc_aud = os.environ.get("OIDC_AUDIENCE")
    if oidc_aud:
        return oidc_aud  # verbatim — must match the IdP's stamped aud exactly
    kc_aud = os.environ.get("KC_MCP_AUDIENCE")
    if kc_aud:
        return kc_aud.rstrip("/")
    base = os.environ.get("PRECIS_BASE_URL", "").rstrip("/")
    return f"{base}/mcp" if base else None


def api_audience() -> str | None:
    """RFC 8707 audience the SPA ``/api`` surface requires in a token's ``aud``.

    Distinct from ``mcp_audience`` on purpose: the ``/mcp`` audience sits on a
    Keycloak *realm-default* client scope, so every token in the realm carries
    it (SPA, Excel add-in, and every anonymous-DCR connector alike).  The API
    audience is stamped on the ``precis-spa`` client *only*, so a connector
    token — which receives realm-default scopes but not the SPA client's
    mappers — lacks it and cannot be replayed against the write/admin/HITL API.

    Resolution order: ``OIDC_API_AUDIENCE`` (mode C — the de-branded name, set
    to whatever the external IdP stamps as the SPA resource audience; used
    verbatim so it matches the token exactly) → legacy ``KC_API_AUDIENCE`` →
    derived ``$PRECIS_BASE_URL/api``.  ``None`` when none is set — API
    audience enforcement is then off (dev with no base URL).
    """
    oidc_aud = os.environ.get("OIDC_API_AUDIENCE")
    if oidc_aud:
        return oidc_aud  # verbatim — must match the IdP's stamped aud exactly
    kc_aud = os.environ.get("KC_API_AUDIENCE")
    if kc_aud:
        return kc_aud.rstrip("/")
    base = os.environ.get("PRECIS_BASE_URL", "").rstrip("/")
    return f"{base}/api" if base else None


def token_has_audience(claims: dict, expected: str) -> bool:
    """True if ``expected`` is present in the token's ``aud`` (str or list)."""
    aud = claims.get("aud")
    aud_list = [aud] if isinstance(aud, str) else (aud or [])
    return expected in aud_list


def require_mcp_audience_when_configured() -> None:
    """Refuse to start a configured multi-user deploy with no `/mcp` audience.

    Without a resolvable audience the RFC 8707 check is silently skipped and
    any signature-valid same-issuer token is honored at `/mcp` — a token
    minted for another relying party of a shared IdP (mode C) would pass.
    An explicitly set ``PRECIS_AUTH_MODE`` marks a real deploy; dev/test
    (nothing set) keeps the audience check optional.
    """
    if not os.environ.get("PRECIS_AUTH_MODE", "").strip():
        return
    if mcp_audience() is None:
        raise RuntimeError(
            "PRECIS_AUTH_MODE is set but no /mcp audience is resolvable. "
            "Set OIDC_AUDIENCE, KC_MCP_AUDIENCE, or PRECIS_BASE_URL — "
            "without one, the RFC 8707 audience check is off and any "
            "same-issuer token is accepted at /mcp."
        )


def require_api_audience_when_configured() -> None:
    """Refuse to start a configured deploy with no SPA ``/api`` audience.

    The mirror of ``require_mcp_audience_when_configured`` for the SPA channel.
    Without a resolvable API audience the middleware's replay guard is silently
    off, so a read-only connector token (which carries the realm-default
    ``/mcp`` audience and the ``precis_user_id`` identity claim) can be replayed
    as a ``Bearer``/cookie against the full write/admin/HITL API.  A set
    ``PRECIS_AUTH_MODE`` marks a real deploy; dev/test keeps the check optional.
    """
    if not os.environ.get("PRECIS_AUTH_MODE", "").strip():
        return
    if api_audience() is None:
        raise RuntimeError(
            "PRECIS_AUTH_MODE is set but no /api audience is resolvable. "
            "Set OIDC_API_AUDIENCE, KC_API_AUDIENCE, or PRECIS_BASE_URL — "
            "without one, a read-only connector token can be replayed "
            "against the SPA write/admin API."
        )


# ---------------------------------------------------------------------------
# Conformance self-check (gap 5) — validate the configured issuer can satisfy
# the /mcp token contract, so a misconfigured mode-C deployment fails with a
# clear message instead of an opaque 401.
# ---------------------------------------------------------------------------


def _fetch_json(url: str) -> dict:
    with httpx.Client(timeout=5.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def check_token_contract(*, fetch: bool = True) -> list[str]:
    """Return a list of human-readable conformance problems (empty = OK).

    Static checks (issuer set, audience configured) always run. Network checks
    (issuer discovery + JWKS reachable, and consistent with the configured
    issuer/JWKS) run when ``fetch=True``.
    """
    problems: list[str] = []
    issuer = config.issuer
    if not issuer:
        problems.append("issuer is not configured (set OIDC_ISSUER or the KC_* vars)")
        return problems
    if mcp_audience() is None:
        problems.append(
            "no /mcp audience configured (OIDC_AUDIENCE / KC_MCP_AUDIENCE / "
            "PRECIS_BASE_URL) — RFC 8707 audience enforcement is OFF"
        )
    if not fetch:
        return problems

    disc_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    disc: dict | None = None
    try:
        disc = _fetch_json(disc_url)
    except Exception as exc:
        problems.append(f"OIDC discovery not reachable at {disc_url}: {exc}")

    jwks_url = config.jwks_url
    kids: set[str] = set()
    try:
        keys = _fetch_json(jwks_url).get("keys", [])
        if not keys:
            problems.append(f"JWKS at {jwks_url} returned no keys")
        kids = {k["kid"] for k in keys if k.get("kid")}
    except Exception as exc:
        problems.append(f"JWKS not reachable at {jwks_url}: {exc}")

    if disc:
        disc_jwks = disc.get("jwks_uri")
        if disc_jwks and disc_jwks != jwks_url and kids:
            # A URL difference alone is expected topology — the verifier
            # fetches keys over the internal address while discovery
            # advertises the public one.  What must hold is that both
            # endpoints serve the same keys.
            try:
                disc_kids = {
                    k["kid"]
                    for k in _fetch_json(disc_jwks).get("keys", [])
                    if k.get("kid")
                }
                if kids.isdisjoint(disc_kids):
                    problems.append(
                        f"configured jwks_url ({jwks_url}) serves none of the "
                        f"keys the issuer advertises at {disc_jwks} — the "
                        "verifier trusts a different key set (wrong realm "
                        "or IdP?)"
                    )
            except Exception as exc:
                problems.append(
                    f"issuer-advertised JWKS not reachable at {disc_jwks}: "
                    f"{exc} — cannot confirm it serves the same keys as the "
                    f"configured jwks_url ({jwks_url})"
                )
        if disc.get("issuer") and disc["issuer"] != issuer:
            problems.append(
                f"discovery issuer ({disc['issuer']}) != configured issuer ({issuer})"
            )
    return problems


# ---------------------------------------------------------------------------
# JWKS-based RS256 validation (PyJWKClient caches keys in-process, refreshes
# on kid miss).
# ---------------------------------------------------------------------------


_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(config.jwks_url, cache_keys=True)
    return _jwks_client


def verify_keycloak_token(token: str) -> dict:
    """Verify a Keycloak-issued JWT and return the claims.

    Raises a ``jwt.PyJWTError`` subclass on any failure (signature, expiry,
    issuer mismatch, missing required claims).  Callers should treat any
    exception as "reject with 401".
    """
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        issuer=config.issuer,
        # No audience check here — this validator is shared by both channels.
        # Each channel enforces its own RFC 8707 audience on the verified
        # claims: the SPA middleware requires the `/api` audience
        # (`api_audience`), the MCP handler the `/mcp` audience
        # (`mcp_audience`).  A single `aud` here could not satisfy both.
        options={
            "require": ["exp", "iat", "sub"],
            "verify_aud": False,
        },
    )


def verify_id_token(id_token: str, expected_nonce: str | None) -> dict:
    """Verify the OIDC id_token and bind it to this login attempt's nonce.

    Unlike the access token (whose aud is the API audience), the id_token's
    aud is the client_id, so it is verified with that audience.  The nonce
    binds the token to the authorize request we initiated: without checking
    it, an id_token captured from a different flow could be replayed.  Raises
    a ``jwt.PyJWTError`` subclass on signature/claim failure or ``ValueError``
    on a missing/mismatched nonce — callers treat either as "reject".
    """
    signing_key = _get_jwks_client().get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        issuer=config.issuer,
        audience=config.client_id,
        options={"require": ["exp", "iat", "sub", "nonce"]},
    )
    claim_nonce = claims.get("nonce", "")
    if not expected_nonce or not hmac.compare_digest(claim_nonce, expected_nonce):
        raise ValueError("OIDC id_token nonce mismatch")
    return claims


# ---------------------------------------------------------------------------
# PKCE helpers (RFC 7636)
# ---------------------------------------------------------------------------


def gen_code_verifier() -> str:
    return base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Authorize URL / token exchange
# ---------------------------------------------------------------------------


def _client_secret_param() -> dict:
    """Token-request client auth for a confidential pre-registered client.

    Empty for a public/PKCE client (the bundled-Keycloak default); carries
    ``client_secret`` when ``OIDC_CLIENT_SECRET`` is set (mode C).
    """
    return {"client_secret": config.client_secret} if config.client_secret else {}


def build_authorize_url(state: str, challenge: str, nonce: str,
                        scope: str = "openid profile email") -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "state": state,
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    return f"{config.authorize_endpoint}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> dict:
    """Server-to-server token exchange.  Returns the parsed JSON response."""
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": config.client_id,
                "code": code,
                "redirect_uri": config.redirect_uri,
                "code_verifier": code_verifier,
                **_client_secret_param(),
            },
        )
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    """Server-to-server refresh-token exchange.  Returns the parsed JSON response.

    The realm rotates refresh tokens (``revokeRefreshToken`` +
    ``refreshTokenMaxReuse: 0``), so the response carries a *new* refresh
    token that the caller must re-store — the one passed in is invalid the
    moment Keycloak answers.
    """
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            config.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": config.client_id,
                "refresh_token": refresh_token,
                **_client_secret_param(),
            },
        )
    resp.raise_for_status()
    return resp.json()


def revoke_keycloak_session(refresh_token: str) -> None:
    """End the user's Keycloak SSO session via the back-channel logout endpoint.

    Posts the refresh token to Keycloak's logout endpoint, which invalidates
    both the refresh token and the SSO cookie's underlying session.  Best
    effort — failures are logged but don't block local cookie clearing.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                config.logout_endpoint,
                data={
                    "client_id": config.client_id,
                    "refresh_token": refresh_token,
                    **_client_secret_param(),
                },
            )
        if resp.status_code not in (200, 204):
            logger.warning(
                "Keycloak logout returned %s: %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Keycloak logout call failed: %s", exc)


# ---------------------------------------------------------------------------
# Cookies + CSRF
# ---------------------------------------------------------------------------


SESSION_COOKIE = "precis_session"
CSRF_COOKIE = "precis_csrf"
REFRESH_COOKIE = "precis_refresh"

def _resolve_cookie_secure(explicit: str | None, base_url: str) -> bool:
    """Secure flag for the session/CSRF/refresh cookies — fails CLOSED on https.

    Local HTTP dev sets ``PRECIS_COOKIE_SECURE=false`` so browsers attach the
    cookie over plain HTTP.  But ``Secure=False`` on an https origin is never
    correct: a deploy that forgets ``PRECIS_COOKIE_SECURE`` must not silently
    serve session cookies without Secure.  So any https public origin forces it
    on regardless of the env var; the explicit value only matters for http dev.
    """
    if (explicit or "").lower() in ("1", "true", "yes"):
        return True
    return base_url.lower().startswith("https://")


COOKIE_SECURE = _resolve_cookie_secure(
    os.environ.get("PRECIS_COOKIE_SECURE"),
    os.environ.get("PRECIS_BASE_URL", ""),
)


def set_session_cookie(response: Response, access_token: str, max_age: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=access_token,
        max_age=max_age,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def set_csrf_cookie(response: Response, max_age: int) -> str:
    """Issue a CSRF token, set it as a JS-readable cookie, return the value."""
    token = pysecrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=max_age,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return token


def set_refresh_cookie(response: Response, refresh_token: str, max_age: int) -> None:
    """Store the OIDC refresh token in an httpOnly cookie scoped to /api/auth/.

    Used at logout time to call Keycloak's back-channel logout endpoint and
    invalidate the SSO session.  Path-restricted so it's never sent on
    non-auth API calls.
    """
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/api/auth/",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    response.delete_cookie(key=CSRF_COOKIE, path="/")
    response.delete_cookie(key=REFRESH_COOKIE, path="/api/auth/")


def extract_session_token(request: Request) -> str | None:
    """Cookie wins over the Authorization header."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def csrf_token_matches(request: Request) -> bool:
    """Double-submit cookie check.

    Constant-time compare: the token is a 256-bit random value, but a plain
    ``==`` short-circuits on the first differing byte and leaks a timing
    side-channel on the comparison.
    """
    header = request.headers.get("X-CSRF-Token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    return bool(header) and hmac.compare_digest(header, cookie)
