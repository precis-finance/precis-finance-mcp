# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Binding-scoped JWT verification for ingestion push / upload endpoints.

These are machine-to-machine tokens: the customer's orchestrator signs
them with a shared secret (``INGEST_BINDING_JWT_SECRET``) to prove
authorisation to push data into a specific binding.  They are issued
outside Précis and never seen by end users.

This is deliberately separate from the user-facing auth (Keycloak OIDC
+ cookie session).  Treating them as one channel would force the
orchestrator to do an OAuth dance per push and surface a service
account in Keycloak that only one machine ever uses.
"""
from __future__ import annotations

import os
from functools import lru_cache

import jwt


class BindingTokenError(Exception):
    """Raised when a binding-scoped token fails verification."""


@lru_cache(maxsize=1)
def _get_secret() -> str:
    val = (os.getenv("INGEST_BINDING_JWT_SECRET") or "").strip()
    if not val:
        raise BindingTokenError(
            "INGEST_BINDING_JWT_SECRET is not set.  The push / upload "
            "endpoints can only verify binding-scoped tokens when this "
            "shared secret is configured (typically via secret manager or "
            "INGEST_BINDING_JWT_SECRET_FILE)."
        )
    return val


def verify_binding_token(token: str) -> dict:
    """Decode and verify a binding-scoped JWT (HS256, shared-secret).

    Returns the claims on success; raises ``BindingTokenError`` on any
    failure (expired, invalid signature, malformed).
    """
    try:
        # `exp` is mandatory: PyJWT only verifies it when present, and a
        # binding token minted without one would be valid forever.
        return jwt.decode(
            token,
            _get_secret(),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise BindingTokenError("Binding token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise BindingTokenError(f"Invalid binding token: {exc}") from exc
