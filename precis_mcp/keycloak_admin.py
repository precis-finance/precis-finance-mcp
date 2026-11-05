# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Thin wrapper over Keycloak's Admin REST API for user provisioning.

Used by the admin routes (`/api/admin/users/*`) and the one-time
user-migration script so user creation, password reset, and
enable/disable go through one place.

Uses a realm-local service account through ``client_credentials``. Operator
credentials are deliberately not accepted here. Tokens refresh once on 401.
"""
from __future__ import annotations

import logging
import os
import secrets as pysecrets
import string
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


APPLICATION_GROUP = "/precis-application-users"

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
    """Lazily authenticated user-provisioning client; token cached per instance."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=10.0)
        self._token: str | None = None
        self._realm = quote(os.environ.get("KC_REALM", "precis"), safe="")

    # ------------------------------------------------------------------ auth
    def _base(self) -> str:
        return os.environ.get(
            "KC_BASE_URL_INTERNAL", "http://localhost:8080/auth"
        ).rstrip("/")

    def _login(self) -> str:
        secret = os.environ.get("KC_PROVISIONING_CLIENT_SECRET", "")
        if not secret and os.environ.get("KC_PROVISIONING_CLIENT_SECRET_FILE"):
            try:
                secret = Path(os.environ["KC_PROVISIONING_CLIENT_SECRET_FILE"]).read_text().rstrip("\r\n")
            except OSError:
                raise KeycloakAdminError("KC_PROVISIONING_CLIENT_SECRET_FILE is not readable") from None
        if not secret:
            raise KeycloakAdminError(
                "KC_PROVISIONING_CLIENT_SECRET is not set — reconcile the "
                "Keycloak provisioning client and supply its secret (or _FILE)."
            )
        resp = self._client.post(
            f"{self._base()}/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ.get("KC_PROVISIONING_CLIENT_ID", "precis-user-provisioner"),
                "client_secret": secret,
            },
        )
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"Keycloak provisioning authentication failed ({resp.status_code})"
            )
        return resp.json()["access_token"]

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = f"{self._base()}/admin/realms/{self._realm}{path}"
        try:
            resp = self._client.request(method, url, headers=self._headers(), **kw)
            if resp.status_code == 401:
                # Authentication rejection is safe to retry; never replay an
                # ambiguous POST timeout or an authorization failure.
                self._token = None
                resp = self._client.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError:
            raise KeycloakAdminError("Keycloak provisioning request failed; check connectivity") from None
        return resp

    # ----------------------------------------------------------------- lookup
    def find_user_id(self, username: str) -> str | None:
        """Return Keycloak's internal user UUID for ``username``, or None."""
        resp = self._request(
            "GET", "/users", params={"username": username, "exact": "true"}
        )
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"Keycloak user lookup failed ({resp.status_code})"
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
            "groups": [APPLICATION_GROUP],
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
                f"Keycloak user creation failed ({resp.status_code})"
            )
        # Keycloak returns the new resource's ID via the Location header.
        loc = resp.headers.get("Location", "")
        return loc.rsplit("/", 1)[-1] if loc else (self.find_user_id(username) or "")

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
                f"Keycloak password reset failed ({resp.status_code})"
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
                f"Keycloak required-action update failed ({resp.status_code})"
            )

    def set_enabled(self, *, username: str, enabled: bool) -> None:
        uuid = self.find_user_id(username)
        if not uuid:
            raise KeycloakAdminError(f"User {username!r} not found in Keycloak")
        resp = self._request("PUT", f"/users/{uuid}", json={"enabled": enabled})
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"Keycloak user status update failed ({resp.status_code})"
            )

    def delete_user(self, username: str) -> None:
        uuid = self.find_user_id(username)
        if not uuid:
            return  # already gone
        resp = self._request("DELETE", f"/users/{uuid}")
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(
                f"Keycloak user deletion failed ({resp.status_code})"
            )
