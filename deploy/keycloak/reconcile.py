#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Realm reconcile for the open bundle — Keycloak Admin REST API, stdlib only.

Runs as a one-shot in the precis-mcp image against the keycloak service. A
network-mode reduction of scripts/keycloak/keycloak_apply_realm.sh that makes
`--import-realm` functional for /mcp: it clears frontendUrl (so KC_HOSTNAME
governs), sets precis-spa redirect URIs, declares the precis_user_id
user-profile attribute (Keycloak 26 drops undeclared attributes),
removes the anonymous-DCR-blocking policies, and maintains the realm-default
precis-mcp client scope: the RFC 8707 aud mapper (restamped when
PRECIS_BASE_URL changes) and the precis_user_id identity mapper for DCR
clients.

No kcadm and no third-party deps — the Keycloak 26 image is ubi-micro (no
package manager), so the reconcile lives in the python image instead.

Env: KC_BASE_URL_INTERNAL, PRECIS_BASE_URL (or KC_BASE_URL_PUBLIC),
KC_MCP_AUDIENCE, KC_REALM, KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

KC = os.environ.get("KC_BASE_URL_INTERNAL", "http://keycloak:8080/auth").rstrip("/")
REALM = os.environ.get("KC_REALM", "precis")
ADMIN_USER = os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD"]
BASE = (os.environ.get("PRECIS_BASE_URL") or "").rstrip("/")
PUBLIC = (os.environ.get("KC_BASE_URL_PUBLIC") or (f"{BASE}/auth" if BASE else "")).rstrip("/")
AUD = os.environ.get("KC_MCP_AUDIENCE") or (f"{BASE}/mcp" if BASE else "")
ORIGIN = PUBLIC[:-5] if PUBLIC.endswith("/auth") else PUBLIC  # SPA origin, no /auth

_token: str | None = None


def _admin(method: str, path: str, data=None) -> Any:
    url = f"{KC}/admin/realms/{REALM}{path}"
    headers = {"Authorization": f"Bearer {_token}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(r) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else None


def _get_token() -> str:
    data = urllib.parse.urlencode({
        "grant_type": "password", "client_id": "admin-cli",
        "username": ADMIN_USER, "password": ADMIN_PASS,
    }).encode()
    url = f"{KC}/realms/master/protocol/openid-connect/token"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST")) as resp:
        return json.loads(resp.read())["access_token"]


def main() -> None:
    global _token

    # Authenticate (retry: a fresh Keycloak takes 20-40s to boot + import).
    print(f"Waiting for Keycloak admin at {KC} ...", flush=True)
    for _ in range(40):
        try:
            _token = _get_token()
            break
        except Exception:
            time.sleep(3)
    else:
        sys.exit("ERROR: Keycloak admin login failed within 120s")

    # Wait for the imported realm.
    for _ in range(20):
        try:
            realm = _admin("GET", "")
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(3)
                continue
            raise
    else:
        sys.exit(f"ERROR: realm {REALM!r} not imported within 60s")
    print(f"Authenticated; realm {REALM!r} present.", flush=True)

    # 1. Realm config: clear frontendUrl (KC_HOSTNAME governs), set login theme.
    realm.setdefault("attributes", {})["frontendUrl"] = ""
    realm["loginTheme"] = "precis"
    _admin("PUT", "", realm)
    print("Realm config reconciled (frontendUrl='', loginTheme=precis).", flush=True)

    # 2. precis-spa redirect URIs / web origins (best-effort — the open MCP
    #    flow uses DCR clients, not the SPA).
    if ORIGIN:
        spa = _admin("GET", "/clients?clientId=precis-spa")
        if spa:
            c = spa[0]
            c["redirectUris"] = [f"{ORIGIN}/*"]
            c["webOrigins"] = [ORIGIN]
            _admin("PUT", f"/clients/{c['id']}", c)
            print(f"precis-spa redirect URIs set to {ORIGIN}/*", flush=True)
        else:
            print("precis-spa client absent — skipping.", flush=True)

    # 3. Declare precis_user_id in the user profile (else Keycloak 26 drops it
    #    and /mcp can't resolve the identity claim).
    prof = _admin("GET", "/users/profile")
    attrs = prof.setdefault("attributes", [])
    existing = {a["name"] for a in attrs}
    added = False
    for name, display in (("precis_user_id", "Précis User ID"),):
        if name in existing:
            continue
        attrs.append({
            "name": name, "displayName": display,
            "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
            "multivalued": False, "group": "user-metadata",
        })
        added = True
    if added:
        groups = prof.setdefault("groups", [])
        if not any(g.get("name") == "user-metadata" for g in groups):
            groups.append({"name": "user-metadata", "displayHeader": "User metadata"})
        _admin("PUT", "/users/profile", prof)
        print("Declared precis_user_id in the user profile.", flush=True)
    else:
        print("User profile already declares precis_user_id.", flush=True)

    # 4. Remove anonymous DCR-blocking policies (external MCP hosts self-register).
    block_pid = {"trusted-hosts", "allowed-client-templates", "consent-required"}
    block_name = {"Trusted Hosts", "Allowed Client Scopes", "Consent Required"}
    comps = _admin(
        "GET",
        "/components?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
    )
    removed = 0
    for c in comps or []:
        if c.get("subType") != "anonymous":
            continue
        if c.get("providerId") in block_pid or c.get("name") in block_name:
            _admin("DELETE", f"/components/{c['id']}")
            print(f"  removed anonymous '{c.get('name', c.get('providerId'))}' policy", flush=True)
            removed += 1
    if not removed:
        print("Anonymous DCR-blocking policies already absent.", flush=True)

    # 5. RFC 8707 audience mapper in a realm-default client scope.
    if AUD:
        print(f"Reconciling precis-mcp audience scope (aud={AUD}) ...", flush=True)
        scopes = _admin("GET", "/client-scopes")
        scope = next((s for s in scopes if s["name"] == "precis-mcp"), None)
        if scope is None:
            _admin("POST", "/client-scopes", {
                "name": "precis-mcp", "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "false",
                               "display.on.consent.screen": "false"},
            })
            scopes = _admin("GET", "/client-scopes")
            scope = next(s for s in scopes if s["name"] == "precis-mcp")
            print("  created client scope precis-mcp", flush=True)
        sid = scope["id"]
        mappers = _admin("GET", f"/client-scopes/{sid}/protocol-mappers/models") or []
        aud_mapper = next((m for m in mappers if m["name"] == "mcp-audience"), None)
        if aud_mapper is None:
            _admin("POST", f"/client-scopes/{sid}/protocol-mappers/models", {
                "name": "mcp-audience", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {"included.custom.audience": AUD,
                           "id.token.claim": "false", "access.token.claim": "true"},
            })
            print("  added mcp-audience mapper", flush=True)
        elif aud_mapper["config"].get("included.custom.audience") != AUD:
            aud_mapper["config"]["included.custom.audience"] = AUD
            _admin("PUT", f"/client-scopes/{sid}/protocol-mappers/models/{aud_mapper['id']}",
                   aud_mapper)
            print(f"  restamped mcp-audience mapper to {AUD}", flush=True)
        # Identity claim for DCR clients: precis-spa carries its own
        # precis_user_id mapper, but dynamically registered clients (claude.ai)
        # only get the realm-default scopes — without this mapper their access
        # tokens lack the claim resolve_user_id needs and /mcp returns 403.
        if not any(m["name"] == "precis_user_id" for m in mappers):
            _admin("POST", f"/client-scopes/{sid}/protocol-mappers/models", {
                "name": "precis_user_id", "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-attribute-mapper",
                "config": {"user.attribute": "precis_user_id",
                           "claim.name": "precis_user_id",
                           "jsonType.label": "String",
                           "id.token.claim": "true",
                           "access.token.claim": "true",
                           "userinfo.token.claim": "true"},
            })
            print("  added precis_user_id identity mapper", flush=True)
        defaults = _admin("GET", "/default-default-client-scopes")
        if not any(d["id"] == sid for d in defaults):
            _admin("PUT", f"/default-default-client-scopes/{sid}", {})
            print("  precis-mcp promoted to realm default client scope", flush=True)
    else:
        print("KC_MCP_AUDIENCE unset — skipping audience mapper (dev posture).", flush=True)

    print(f"Realm reconciled for {ORIGIN or '(no public origin)'}.", flush=True)


if __name__ == "__main__":
    main()
