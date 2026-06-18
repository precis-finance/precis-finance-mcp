# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Thin wrapper over Keycloak's Admin REST API for user provisioning.

Used by the admin routes (`/api/admin/users/*`) and the one-time
user-migration script so user creation, password reset, and
enable/disable go through one place.

Reads ``KC_BASE_URL_INTERNAL``, ``KC_BOOTSTRAP_ADMIN_USERNAME`` (defaults
to ``admin``), and ``KC_BOOTSTRAP_ADMIN_PASSWORD`` from the environment
when the first call is made.  Sessions cache an admin access token
and refresh on 401.
"""
from __future__ import annotations

import logging
import os
import secrets as pysecrets
import string
from typing import Any

import httpx

logger = logging.getLogger(__name__)


KC_REALM = "precis"
KC_ADMIN_REALM = "master"

_TEMP_PW_ALPHABET = string.ascii_letters + string.digits


class KeycloakAdminError(Exception):
    """Raised when a Keycloak Admin API call fails."""


def temp_password(length: int = 16) -> str:
    """Generate a URL-safe-ish temporary password (no special chars)."""
    return "".join(pysecrets.choice(_TEMP_PW_ALPHABET) for _ in range(length))


def _split_name(name: str | None) -> tuple[str, str]:
    if not name:
        return "", ""
    parts = name.strip().split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


class KeycloakAdmin:
    """Per-process Keycloak Admin API client.

    Constructs lazily and caches an admin access token across calls.
    Single-tenant: only the ``precis`` realm is targeted.
    """

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=10.0)
        self._token: str | None = None

    # ------------------------------------------------------------------ auth
    def _base(self) -> str:
        return os.environ.get(
            "KC_BASE_URL_INTERNAL", "http://localhost:8080/auth"
        ).rstrip("/")

    def _login(self) -> str:
        username = os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        password = (os.environ.get("KC_BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
        if not password:
            raise KeycloakAdminError(
                "KC_BOOTSTRAP_ADMIN_PASSWORD is not set — cannot authenticate "
                "against Keycloak Admin API."
            )
        resp = self._client.post(
            f"{self._base()}/realms/{KC_ADMIN_REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
        )
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"Keycloak admin login failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()["access_token"]

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = f"{self._base()}/admin/realms/{KC_REALM}{path}"
        resp = self._client.request(method, url, headers=self._headers(), **kw)
        if resp.status_code == 401:
            # Token expired — refresh once and retry.
            self._token = None
            resp = self._client.request(method, url, headers=self._headers(), **kw)
        return resp

    # ----------------------------------------------------------------- lookup
    def find_user_id(self, username: str) -> str | None:
        """Return Keycloak's internal user UUID for ``username``, or None."""
        resp = self._request(
            "GET", "/users", params={"username": username, "exact": "true"}
        )
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"find_user_id({username!r}) failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        rows = resp.json()
        if not rows:
            return None
        return rows[0]["id"]

    # ----------------------------------------------------------------- create
    def create_user(
        self,
        *,
        username: str,
        precis_user_id: str,
        password: str,
        email: str | None = None,
        name: str | None = None,
        temporary: bool = True,
    ) -> str:
        """Provision a new user in the precis realm.  Returns the Keycloak UUID.

        ``temporary=True`` flags the password as one-time so Keycloak forces
        UPDATE_PASSWORD on first login.  Set ``temporary=False`` for tests
        or scripted accounts that won't go through the reset flow.
        """
        first, last = _split_name(name)
        body = {
            "username": username,
            "enabled": True,
            "emailVerified": bool(email),
            "email": email or "",
            "firstName": first,
            "lastName": last,
            "attributes": {
                "precis_user_id": [precis_user_id],
            },
            "credentials": [
                {"type": "password", "value": password, "temporary": temporary},
            ],
            "requiredActions": ["UPDATE_PASSWORD"] if temporary else [],
        }
        resp = self._request("POST", "/users", json=body)
        if resp.status_code == 409:
            raise KeycloakAdminError(
                f"User {username!r} already exists in Keycloak"
            )
        if resp.status_code != 201:
            raise KeycloakAdminError(
                f"create_user({username!r}) failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        # Keycloak returns the new resource's ID via the Location header.
        loc = resp.headers.get("Location", "")
        return loc.rsplit("/", 1)[-1] if loc else (self.find_user_id(username) or "")

    def ensure_client(
        self,
        *,
        client_id: str,
        public: bool = True,
        direct_access_grants: bool = False,
        standard_flow: bool = True,
        name: str | None = None,
    ) -> None:
        """Create an OIDC client if it doesn't already exist (idempotent)."""
        resp = self._request("GET", "/clients", params={"clientId": client_id})
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"client lookup {client_id!r} failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        if resp.json():
            return
        body = {
            "clientId": client_id,
            "name": name or client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": public,
            "standardFlowEnabled": standard_flow,
            "directAccessGrantsEnabled": direct_access_grants,
            "serviceAccountsEnabled": False,
        }
        resp = self._request("POST", "/clients", json=body)
        if resp.status_code not in (201, 409):
            raise KeycloakAdminError(
                f"ensure_client({client_id!r}) failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )

    # ----------------------------------------------------------------- update
    def reset_password(
        self,
        *,
        username: str,
        password: str,
        temporary: bool = True,
    ) -> None:
        """Replace the user's password (idempotent for the admin)."""
        uuid = self.find_user_id(username)
        if not uuid:
            raise KeycloakAdminError(f"User {username!r} not found in Keycloak")
        resp = self._request(
            "PUT",
            f"/users/{uuid}/reset-password",
            json={"type": "password", "value": password, "temporary": temporary},
        )
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"reset_password({username!r}) failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )

    def clear_required_actions(self, username: str) -> None:
        """Drop all required actions (e.g. UPDATE_PASSWORD) for the user.

        Call after planting a known, non-temporary password so the next
        login isn't gated on the reset flow.
        """
        uuid = self.find_user_id(username)
        if not uuid:
            raise KeycloakAdminError(f"User {username!r} not found in Keycloak")
        resp = self._request("PUT", f"/users/{uuid}", json={"requiredActions": []})
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"clear_required_actions({username!r}) failed "
                f"({resp.status_code}): {resp.text[:200]}"
            )

    def set_enabled(self, *, username: str, enabled: bool) -> None:
        uuid = self.find_user_id(username)
        if not uuid:
            raise KeycloakAdminError(f"User {username!r} not found in Keycloak")
        resp = self._request("PUT", f"/users/{uuid}", json={"enabled": enabled})
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"set_enabled({username!r}, {enabled}) failed "
                f"({resp.status_code}): {resp.text[:200]}"
            )

    def delete_user(self, username: str) -> None:
        uuid = self.find_user_id(username)
        if not uuid:
            return  # already gone
        resp = self._request("DELETE", f"/users/{uuid}")
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"delete_user({username!r}) failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
