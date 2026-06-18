# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Install-time auth-mode selector (`PRECIS_AUTH_MODE`).

One env var names the deployment's identity posture, so the choice is a single
line and the runtime can fail fast when the selected mode's required config is
missing (rather than surfacing as an opaque 401 later). See
``deployment/oauth-keycloak.md``.

Modes:
- ``devkey``   — single-user local trial; served by ``precis_mcp.server``
                 (the 3-gate dev guard owns the deeper checks).
- ``keycloak`` — multi-user, bundled Keycloak (optionally federated upstream).
- ``oidc``     — multi-user, direct external OIDC trust (no bundled Keycloak);
                 requires ``OIDC_ISSUER`` (see oidc.py issuer override).
- ``header``   — reserved for mode D (trusted-header / reverse-proxy); not yet
                 supported.

This module only *resolves and validates* the mode. Which Compose profile/bundle
comes up is the deploy layer's job; the env var is the contract between them.
"""
from __future__ import annotations

import enum
import os


class AuthModeError(RuntimeError):
    """Raised when PRECIS_AUTH_MODE is invalid or its required config is missing."""


class AuthMode(str, enum.Enum):
    DEVKEY = "devkey"
    KEYCLOAK = "keycloak"
    OIDC = "oidc"


# Multi-user entrypoint (app_open) accepts these; devkey runs via server.py.
MULTI_USER_MODES = frozenset({AuthMode.KEYCLOAK, AuthMode.OIDC})

# Allow-listed `users` columns the configurable identity mapping may join on
# (PRECIS_IDENTITY_COLUMN). The value is interpolated into SQL, so it MUST come
# from this set, never from request input. `id` = the claim value is the user_id
# directly; `external_id` = look it up (added by migration open/004).
IDENTITY_COLUMN_ALLOWLIST = frozenset({"id", "external_id"})


def resolve_auth_mode(*, default: AuthMode | None = None) -> AuthMode:
    """Resolve ``PRECIS_AUTH_MODE`` to an ``AuthMode``.

    Empty/unset falls back to ``default`` when one is given (dev/test
    tolerance), else raises. ``header`` is recognised but rejected (mode D is
    unbuilt). An unknown value raises.
    """
    raw = os.environ.get("PRECIS_AUTH_MODE", "").strip().lower()
    if not raw:
        if default is not None:
            return default
        raise AuthModeError(
            "PRECIS_AUTH_MODE is not set; expected one of "
            f"{[m.value for m in AuthMode]}"
        )
    if raw == "header":
        raise AuthModeError(
            "PRECIS_AUTH_MODE='header' (mode D, trusted-header/reverse-proxy) "
            "is not yet supported"
        )
    try:
        return AuthMode(raw)
    except ValueError:
        raise AuthModeError(
            f"PRECIS_AUTH_MODE={raw!r} is invalid; expected one of "
            f"{[m.value for m in AuthMode]}"
        ) from None


def validate_auth_config(mode: AuthMode) -> None:
    """Fail fast if the env required by ``mode`` is missing.

    Only hard-requires what has no sensible dev default:
    - ``oidc`` needs ``OIDC_ISSUER`` (pointing at an external IdP has no default).
    - ``devkey`` needs ``MCP_DEV_KEY`` (also enforced by server.py's guard).
    - ``keycloak`` has localhost dev defaults, so nothing is hard-required here.
    """
    if mode is AuthMode.OIDC and not os.environ.get("OIDC_ISSUER"):
        raise AuthModeError(
            "PRECIS_AUTH_MODE=oidc requires OIDC_ISSUER (the external IdP issuer "
            "URL); set OIDC_JWKS_URL too if it is not derivable from discovery"
        )
    if mode is AuthMode.DEVKEY and not (os.environ.get("MCP_DEV_KEY") or "").strip():
        raise AuthModeError("PRECIS_AUTH_MODE=devkey requires MCP_DEV_KEY")

    # Identity-claim mapping (gap 2) — fail fast on a bad join column rather than
    # per request. Applies to any multi-user mode.
    column = (os.environ.get("PRECIS_IDENTITY_COLUMN", "").strip() or "id")
    if column not in IDENTITY_COLUMN_ALLOWLIST:
        raise AuthModeError(
            f"PRECIS_IDENTITY_COLUMN={column!r} is not an allowed identity column "
            f"({sorted(IDENTITY_COLUMN_ALLOWLIST)})"
        )


def resolve_for_multiuser(*, default: AuthMode = AuthMode.KEYCLOAK) -> AuthMode:
    """Resolve + validate for the multi-user entrypoint (app_open).

    Rejects ``devkey`` (that is the single-user ``precis_mcp.server`` path) and
    validates the selected mode's required config. Returns the resolved mode.
    """
    mode = resolve_auth_mode(default=default)
    if mode not in MULTI_USER_MODES:
        raise AuthModeError(
            f"PRECIS_AUTH_MODE={mode.value!r} is single-user; the multi-user "
            "server (app_open) accepts only "
            f"{sorted(m.value for m in MULTI_USER_MODES)}. "
            "Run the dev-key trial via `python -m precis_mcp.server`."
        )
    validate_auth_config(mode)
    return mode


def resolve_for_devkey() -> AuthMode:
    """Validate the mode for the single-user dev server (``precis_mcp.server``).

    Symmetric with ``resolve_for_multiuser``: unset → ``devkey``; a multi-user
    mode (`keycloak` / `oidc`) means this is the wrong entrypoint, so it raises —
    a B/C-configured host cannot accidentally start the no-auth dev server.
    Returns the resolved mode (always ``DEVKEY`` on success).
    """
    mode = resolve_auth_mode(default=AuthMode.DEVKEY)
    if mode is not AuthMode.DEVKEY:
        raise AuthModeError(
            f"PRECIS_AUTH_MODE={mode.value!r} selects the multi-user server "
            "(`python -m precis_mcp.app_open`); the single-user dev server runs "
            "only in devkey mode."
        )
    return mode
