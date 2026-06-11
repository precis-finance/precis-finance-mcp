# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for PRECIS_BASE_URL-derived Keycloak config (precis_mcp/oidc.py).

The single deployment-host knob PRECIS_BASE_URL derives the public-facing
OIDC URLs; each derived value is overridable by its explicit KC_* var
(explicit wins).  For mode C (direct external OIDC trust) the OIDC_ISSUER /
OIDC_JWKS_URL / OIDC_AUDIENCE overrides point the verifier at a non-Keycloak
issuer, winning over the Keycloak-derived path.
"""
from __future__ import annotations

import pytest

from precis_mcp import oidc

_HOST_VARS = (
    "PRECIS_BASE_URL",
    "KC_BASE_URL_PUBLIC",
    "KC_REDIRECT_URI",
    "KC_MCP_AUDIENCE",
    "KC_BASE_URL_INTERNAL",
    "OIDC_ISSUER",
    "OIDC_JWKS_URL",
    "OIDC_AUDIENCE",
    "KC_CLIENT_ID",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
)


def _cfg(monkeypatch, **env):
    for k in _HOST_VARS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return oidc._config_from_env()


def test_derives_public_and_redirect_from_base(monkeypatch):
    cfg = _cfg(monkeypatch, PRECIS_BASE_URL="https://demo.precis.finance")
    assert cfg.base_url_public == "https://demo.precis.finance/auth"
    assert cfg.redirect_uri == "https://demo.precis.finance/api/auth/callback"
    assert cfg.issuer == "https://demo.precis.finance/auth/realms/precis"


def test_trailing_slash_on_base_is_stripped(monkeypatch):
    cfg = _cfg(monkeypatch, PRECIS_BASE_URL="https://demo.precis.finance/")
    assert cfg.base_url_public == "https://demo.precis.finance/auth"


def test_explicit_public_overrides_base(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        PRECIS_BASE_URL="https://demo.precis.finance",
        KC_BASE_URL_PUBLIC="https://other.example/kc",
    )
    assert cfg.base_url_public == "https://other.example/kc"
    # redirect still derived from base — overrides are independent
    assert cfg.redirect_uri == "https://demo.precis.finance/api/auth/callback"


def test_dev_default_when_no_base(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert cfg.base_url_public == "http://localhost/auth"
    assert cfg.redirect_uri == "http://localhost/api/auth/callback"


def test_internal_url_is_not_host_derived(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        PRECIS_BASE_URL="https://demo.precis.finance",
        KC_BASE_URL_INTERNAL="http://keycloak:8080/auth",
    )
    assert cfg.base_url_internal == "http://keycloak:8080/auth"


def test_mcp_audience_derived_from_base(monkeypatch):
    for k in _HOST_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo.precis.finance")
    assert oidc.mcp_audience() == "https://demo.precis.finance/mcp"


def test_mcp_audience_explicit_wins(monkeypatch):
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo.precis.finance")
    monkeypatch.setenv("KC_MCP_AUDIENCE", "https://x.example/mcp")
    assert oidc.mcp_audience() == "https://x.example/mcp"


def test_mcp_audience_none_when_unset(monkeypatch):
    for k in _HOST_VARS:
        monkeypatch.delenv(k, raising=False)
    assert oidc.mcp_audience() is None


# --- Mode C: external-IdP issuer / JWKS / audience overrides ---------------


def test_oidc_issuer_override_wins_verbatim(monkeypatch):
    # Used as-is, including a trailing slash (Auth0's iss ends in "/"), since
    # the iss claim must match the token exactly.
    cfg = _cfg(
        monkeypatch,
        PRECIS_BASE_URL="https://demo.precis.finance",
        OIDC_ISSUER="https://tenant.auth0.com/",
    )
    assert cfg.issuer == "https://tenant.auth0.com/"
    assert "realms" not in cfg.issuer  # Keycloak-derived path fully bypassed


def test_oidc_jwks_url_override_wins(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        PRECIS_BASE_URL="https://demo.precis.finance",
        OIDC_JWKS_URL="https://tenant.auth0.com/.well-known/jwks.json",
    )
    assert cfg.jwks_url == "https://tenant.auth0.com/.well-known/jwks.json"


def test_no_oidc_override_uses_keycloak_path(monkeypatch):
    cfg = _cfg(monkeypatch, PRECIS_BASE_URL="https://demo.precis.finance")
    assert cfg.issuer == "https://demo.precis.finance/auth/realms/precis"
    assert cfg.jwks_url.endswith("/realms/precis/protocol/openid-connect/certs")


def test_empty_oidc_issuer_falls_back_to_derived(monkeypatch):
    # An empty env var must not shadow the Keycloak-derived issuer.
    cfg = _cfg(
        monkeypatch,
        PRECIS_BASE_URL="https://demo.precis.finance",
        OIDC_ISSUER="",
    )
    assert cfg.issuer == "https://demo.precis.finance/auth/realms/precis"


def test_mcp_audience_oidc_audience_wins_verbatim(monkeypatch):
    for k in _HOST_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo.precis.finance")
    monkeypatch.setenv("KC_MCP_AUDIENCE", "https://x.example/mcp")
    monkeypatch.setenv("OIDC_AUDIENCE", "https://mcp.example/resource/")
    # OIDC_AUDIENCE wins over the legacy var and is used verbatim (trailing
    # slash preserved — it must match the IdP's stamped aud exactly).
    assert oidc.mcp_audience() == "https://mcp.example/resource/"


# --- gap 4: pre-registered client id / secret ------------------------------


def test_client_id_default(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert cfg.client_id == "precis-spa"
    assert cfg.client_secret is None


def test_oidc_client_id_override(monkeypatch):
    cfg = _cfg(monkeypatch, OIDC_CLIENT_ID="precis-mcp-prod")
    assert cfg.client_id == "precis-mcp-prod"


def test_oidc_client_id_wins_over_kc(monkeypatch):
    cfg = _cfg(monkeypatch, KC_CLIENT_ID="kc-client", OIDC_CLIENT_ID="oidc-client")
    assert cfg.client_id == "oidc-client"


def test_oidc_client_secret_set(monkeypatch):
    cfg = _cfg(monkeypatch, OIDC_CLIENT_SECRET="s3cr3t")
    assert cfg.client_secret == "s3cr3t"


def test_client_secret_param_helper(monkeypatch):
    base = dict(
        base_url_internal="x", base_url_public="x", realm="r",
        client_id="c", redirect_uri="r",
    )
    monkeypatch.setattr(oidc, "config", oidc.KeycloakConfig(**base, client_secret="s3cr3t"))
    assert oidc._client_secret_param() == {"client_secret": "s3cr3t"}
    monkeypatch.setattr(oidc, "config", oidc.KeycloakConfig(**base))
    assert oidc._client_secret_param() == {}


# --- gap 5: conformance self-check (check_token_contract) -------------------


def _set_config(monkeypatch, **overrides):
    base = dict(
        base_url_internal="http://kc:8080/auth", base_url_public="https://demo/auth",
        realm="precis", client_id="c", redirect_uri="r",
    )
    base.update(overrides)
    monkeypatch.setattr(oidc, "config", oidc.KeycloakConfig(**base))


def _no_audience(monkeypatch):
    for k in ("OIDC_AUDIENCE", "KC_MCP_AUDIENCE", "PRECIS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_conformance_static_flags_missing_audience(monkeypatch):
    _no_audience(monkeypatch)
    _set_config(monkeypatch)
    problems = oidc.check_token_contract(fetch=False)
    assert any("audience" in p for p in problems)


def test_conformance_fetch_ok(monkeypatch):
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo")  # audience derivable
    _set_config(
        monkeypatch,
        issuer_override="https://idp.example/",
        jwks_url_override="https://idp.example/jwks",
    )

    def fake_fetch(url):
        if "openid-configuration" in url:
            return {"issuer": "https://idp.example/", "jwks_uri": "https://idp.example/jwks"}
        return {"keys": [{"kid": "k1"}]}

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    assert oidc.check_token_contract() == []


def test_conformance_jwks_unreachable(monkeypatch):
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo")
    _set_config(
        monkeypatch,
        issuer_override="https://idp.example/",
        jwks_url_override="https://idp.example/jwks",
    )

    def fake_fetch(url):
        if "openid-configuration" in url:
            return {"issuer": "https://idp.example/", "jwks_uri": "https://idp.example/jwks"}
        raise RuntimeError("connection refused")

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    assert any("JWKS not reachable" in p for p in oidc.check_token_contract())


def test_conformance_jwks_uri_mismatch(monkeypatch):
    monkeypatch.setenv("PRECIS_BASE_URL", "https://demo")
    _set_config(
        monkeypatch,
        issuer_override="https://idp.example/",
        jwks_url_override="https://idp.example/wrong",
    )

    def fake_fetch(url):
        if "openid-configuration" in url:
            return {"issuer": "https://idp.example/", "jwks_uri": "https://idp.example/jwks"}
        return {"keys": [{"kid": "k1"}]}

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    assert any("jwks_uri" in p for p in oidc.check_token_contract())


# --- cookie Secure flag fails closed on https (audit NEW-2) -----------------


def test_cookie_secure_explicit_true():
    assert oidc._resolve_cookie_secure("true", "") is True
    assert oidc._resolve_cookie_secure("1", "http://localhost") is True


def test_cookie_secure_http_dev_stays_off():
    # The only case the flag is allowed off: an explicit opt-out on http dev.
    assert oidc._resolve_cookie_secure("false", "http://localhost") is False
    assert oidc._resolve_cookie_secure(None, "http://localhost") is False
    assert oidc._resolve_cookie_secure(None, "") is False


def test_cookie_secure_https_origin_forces_on_even_when_env_forgotten():
    # The fail-closed guard: an https public origin must serve Secure cookies
    # even if PRECIS_COOKIE_SECURE is unset, so a prod deploy can't silently
    # ship session cookies without Secure.
    assert oidc._resolve_cookie_secure(None, "https://demo.precis.finance") is True
    assert oidc._resolve_cookie_secure("", "https://demo.precis.finance") is True


def test_cookie_secure_https_origin_overrides_explicit_false():
    # Secure=False on https is never correct; the origin wins.
    assert oidc._resolve_cookie_secure("false", "https://demo.precis.finance") is True


# --- id_token nonce binding (audit NEW-3) -----------------------------------


def _patch_idtoken_decode(monkeypatch, claims):
    """Stub the JWKS lookup + signature verify so the test exercises only the
    nonce-binding branch of verify_id_token (signature/aud are jwt.decode's job
    and tested by PyJWT itself)."""
    class _Key:
        key = "k"

    class _Jwks:
        def get_signing_key_from_jwt(self, _tok):
            return _Key()

    monkeypatch.setattr(oidc, "_get_jwks_client", lambda: _Jwks())
    monkeypatch.setattr(oidc.jwt, "decode", lambda *a, **k: claims)


def test_verify_id_token_accepts_matching_nonce(monkeypatch):
    _patch_idtoken_decode(monkeypatch, {"sub": "u1", "nonce": "abc123"})
    assert oidc.verify_id_token("id-tok", "abc123")["sub"] == "u1"


def test_verify_id_token_rejects_mismatched_nonce(monkeypatch):
    _patch_idtoken_decode(monkeypatch, {"sub": "u1", "nonce": "abc123"})
    with pytest.raises(ValueError):
        oidc.verify_id_token("id-tok", "different")


def test_verify_id_token_rejects_when_expected_nonce_absent(monkeypatch):
    # A login attempt with no stored nonce must not be satisfiable by any token.
    _patch_idtoken_decode(monkeypatch, {"sub": "u1", "nonce": "abc123"})
    with pytest.raises(ValueError):
        oidc.verify_id_token("id-tok", None)


class TestRequireMcpAudienceWhenConfigured:
    """A configured deploy (PRECIS_AUTH_MODE set) must resolve an /mcp
    audience, else the RFC 8707 check would be silently off."""

    def test_unset_auth_mode_is_tolerant(self, monkeypatch):
        from precis_mcp.oidc import require_mcp_audience_when_configured

        for var in ("PRECIS_AUTH_MODE", "OIDC_AUDIENCE",
                    "KC_MCP_AUDIENCE", "PRECIS_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        require_mcp_audience_when_configured()  # no raise

    def test_configured_mode_without_audience_refuses(self, monkeypatch):
        import pytest

        from precis_mcp.oidc import require_mcp_audience_when_configured

        monkeypatch.setenv("PRECIS_AUTH_MODE", "oidc")
        for var in ("OIDC_AUDIENCE", "KC_MCP_AUDIENCE", "PRECIS_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(RuntimeError, match="audience"):
            require_mcp_audience_when_configured()

    def test_configured_mode_with_base_url_passes(self, monkeypatch):
        from precis_mcp.oidc import require_mcp_audience_when_configured

        monkeypatch.setenv("PRECIS_AUTH_MODE", "keycloak")
        monkeypatch.setenv("PRECIS_BASE_URL", "https://demo.example.com")
        require_mcp_audience_when_configured()  # no raise
