# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Operator-only realm administration. Never imported by application routes.

The callable passed to provisioning reconciliation uses realm-relative paths.
HTTP errors expose status only, because upstream bodies may contain secrets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

AdminRequest = Callable[..., Any]
GROUP_NAME = "precis-application-users"
POLICY_NAME = "precis-provisioner"
PERMISSION_NAME = "precis-provision-app-members"


def provisioning_secret() -> str:
    value = os.environ.get("KC_PROVISIONING_CLIENT_SECRET", "")
    if not value and os.environ.get("KC_PROVISIONING_CLIENT_SECRET_FILE"):
        value = Path(os.environ["KC_PROVISIONING_CLIENT_SECRET_FILE"]).read_text().rstrip("\r\n")
    if not value:
        raise RuntimeError("Supply KC_PROVISIONING_CLIENT_SECRET or its _FILE before reconciliation")
    return value


class KeycloakOperator:
    """Explicit password-grant operator session, for deployment scripts only."""

    def __init__(self) -> None:
        self.base = os.environ.get("KC_BASE_URL_INTERNAL", "http://localhost:8080/auth").rstrip("/")
        self.realm = quote(os.environ.get("KC_REALM", "precis"), safe="")
        self.token: str | None = None

    def _send(self, method: str, path: str, data=None, *, form=False):
        body = None if data is None else (urlencode(data) if form else json.dumps(data)).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with urlopen(Request(self.base + path, data=body, headers=headers, method=method), timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raise RuntimeError(f"Keycloak operator request failed ({exc.code})") from None
        except URLError:
            raise RuntimeError("Keycloak operator connection failed") from None

    def request(self, method: str, path: str, data=None):
        if self.token is None:
            password = os.environ.get("KC_BOOTSTRAP_ADMIN_PASSWORD", "")
            if not password and os.environ.get("KC_BOOTSTRAP_ADMIN_PASSWORD_FILE"):
                password = Path(os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD_FILE"]).read_text().rstrip("\r\n")
            if not password:
                raise RuntimeError("Operator credentials required for realm administration")
            self.token = self._send("POST", "/realms/master/protocol/openid-connect/token", {
                "grant_type": "password", "client_id": "admin-cli",
                "username": os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin"), "password": password,
            }, form=True)["access_token"]
        return self._send(method, f"/admin/realms/{self.realm}{path}", data)

    def ensure_client(self, *, client_id: str, public=True, direct_access_grants=False,
                      standard_flow=True, name=None) -> None:
        if self.request("GET", "/clients?" + urlencode({"clientId": client_id})):
            return
        self.request("POST", "/clients", {
            "clientId": client_id, "name": name or client_id, "enabled": True,
            "protocol": "openid-connect", "publicClient": public,
            "standardFlowEnabled": standard_flow, "directAccessGrantsEnabled": direct_access_grants,
            "serviceAccountsEnabled": False,
        })


def _client(admin: AdminRequest, name: str) -> dict:
    rows = admin("GET", "/clients?" + urlencode({"clientId": name}))
    if len(rows) != 1:
        raise RuntimeError(f"Expected exactly one Keycloak client: {name}")
    return rows[0]


def application_group(admin: AdminRequest) -> dict:
    groups = admin("GET", "/groups?" + urlencode({"search": GROUP_NAME, "exact": "true"}))
    if len(groups) != 1 or groups[0].get("path") != "/" + GROUP_NAME:
        raise RuntimeError("Expected one top-level precis-application-users group")
    group = groups[0]
    if admin("GET", f"/groups/{group['id']}/children?max=1"):
        raise RuntimeError("Application enrollment group must not have subgroups")
    mappings = admin("GET", f"/groups/{group['id']}/role-mappings")
    if mappings.get("realmMappings") or mappings.get("clientMappings"):
        raise RuntimeError("Application enrollment group must not grant roles")
    return group


def _service_account_ids(admin: AdminRequest) -> set[str]:
    # GET /users/{id} does not serialize the service-account client link in
    # Keycloak 26.7. Resolve links from clients, rather than trusting usernames.
    ids = set()
    first = 0
    while True:
        clients = admin("GET", f"/clients?first={first}&max=100")
        for client in clients:
            if client.get("serviceAccountsEnabled"):
                ids.add(admin("GET", f"/clients/{client['id']}/service-account-user")["id"])
        if len(clients) < 100:
            return ids
        first += 100


def _check_application_user(admin: AdminRequest, uid: str, management_id: str, service_accounts: set[str]) -> dict:
    user = admin("GET", f"/users/{uid}")
    roles = admin("GET", f"/users/{uid}/role-mappings/clients/{management_id}/composite")
    if uid in service_accounts or roles:
        raise RuntimeError("Refusing enrollment of a Keycloak administrator or service account")
    return user


def _check_owned_policies(admin: AdminRequest) -> None:
    authz = f"/clients/{_client(admin, 'admin-permissions')['id']}/authz/resource-server"
    # This bounded configuration deliberately does not evaluate arbitrary FGAP
    # graphs. Refuse unrelated grants, including role-free delegated admins,
    # rather than silently overwrite them or assume this actor cannot match.
    for policy in admin("GET", authz + "/policy?max=-1"):
        expected = {POLICY_NAME: "user", PERMISSION_NAME: "scope"}.get(policy["name"])
        if expected is None or policy["type"] != expected:
            raise RuntimeError("Competing fine-grained admin policy requires operator review")


def _check_defaults(admin: AdminRequest, realm: dict, group_id: str, management_id: str) -> None:
    # New application users must not inherit administration through global
    # defaults; service/admin identities must not be automatically enrolled.
    default_role = realm.get("defaultRole")
    if not default_role:
        raise RuntimeError("Cannot validate the realm default role")
    roots = [default_role]
    for group in admin("GET", "/default-groups"):
        if group["id"] == group_id:
            raise RuntimeError("Application enrollment must not be a realm default group")
        group = admin("GET", "/groups/" + group["id"])
        while group:
            mappings = admin("GET", f"/groups/{group['id']}/role-mappings")
            roots.extend(mappings.get("realmMappings", []))
            for client in mappings.get("clientMappings", {}).values():
                roots.extend(client["mappings"])
            group = admin("GET", "/groups/" + group["parentId"]) if group.get("parentId") else None
    visited = set()
    while roots:
        role = roots.pop()
        if role["id"] in visited:
            continue
        visited.add(role["id"])
        role = admin("GET", "/roles-by-id/" + role["id"])
        if role.get("clientRole") and role.get("containerId") == management_id:
            raise RuntimeError("Realm defaults must not grant Keycloak administration")
        if role.get("composite"):
            roots.extend(admin("GET", f"/roles-by-id/{role['id']}/composites"))


def enroll_application_user(admin: AdminRequest, username: str) -> None:
    """Explicit operator migration of an existing, confirmed platform account."""
    _check_owned_policies(admin)
    rows = admin("GET", "/users?" + urlencode({"username": username, "exact": "true"}))
    if len(rows) != 1:
        raise RuntimeError(f"Existing Keycloak user not found: {username}")
    uid = rows[0]["id"]
    management_id = _client(admin, "realm-management")["id"]
    user = _check_application_user(admin, uid, management_id, _service_account_ids(admin))
    mapped = user.get("attributes", {}).get("precis_user_id")
    if mapped and mapped != [username]:
        raise RuntimeError("Existing Keycloak identity does not match the platform user")
    group = application_group(admin)
    _check_defaults(admin, admin("GET", ""), group["id"], management_id)
    admin("PUT", f"/users/{uid}/groups/{group['id']}")


def reconcile_provisioner(admin: AdminRequest, secret: str) -> None:
    """Reconcile the owned FGAP V2 policy. Secret comes from stable operator storage.

    No manage-users role: it bypasses fine-grained permissions. The group policy
    grants create-with-group, view and manage members, but no user-side group
    membership or role-mapping authority. Trusted operators own enrollment.
    """
    if not secret:
        raise RuntimeError("Empty provisioning secret")
    client_name = os.environ.get("KC_PROVISIONING_CLIENT_ID", "precis-user-provisioner")
    clients = admin("GET", "/clients?" + urlencode({"clientId": client_name}))
    if clients:
        # A failed validation/repair leaves new token issuance disabled.
        admin("PUT", f"/clients/{clients[0]['id']}", {"enabled": False})
    realm = admin("GET", "")
    realm["adminPermissionsEnabled"] = True
    admin("PUT", "", realm)
    _check_owned_policies(admin)
    groups = admin("GET", "/groups?" + urlencode({"search": GROUP_NAME, "exact": "true"}))
    if not groups:
        admin("POST", "/groups", {"name": GROUP_NAME})
    group = application_group(admin)
    management_id = _client(admin, "realm-management")["id"]
    _check_defaults(admin, realm, group["id"], management_id)
    service_accounts = _service_account_ids(admin)
    first = 0
    while True:
        members = admin("GET", f"/groups/{group['id']}/members?first={first}&max=100")
        for member in members:
            _check_application_user(admin, member["id"], management_id, service_accounts)
        if len(members) < 100:
            break
        first += 100

    desired = {
        "clientId": client_name, "enabled": False, "protocol": "openid-connect",
        "publicClient": False, "clientAuthenticatorType": "client-secret", "secret": secret,
        "serviceAccountsEnabled": True, "standardFlowEnabled": False,
        "implicitFlowEnabled": False, "directAccessGrantsEnabled": False,
        "fullScopeAllowed": False, "authorizationServicesEnabled": False,
        "attributes": {"standard.token.exchange.enabled": "false"},
    }
    if not clients:
        admin("POST", "/clients", desired)
    else:
        admin("PUT", f"/clients/{clients[0]['id']}", desired)
    cid = _client(admin, client_name)["id"]
    cpath = f"/clients/{cid}"
    # Remove inherited MCP scopes and identity/audience mappers, including
    # optional assignments. Only an explicit client-role mapper is installed.
    for kind in ("default", "optional"):
        for scope in admin("GET", f"{cpath}/{kind}-client-scopes"):
            admin("DELETE", f"{cpath}/{kind}-client-scopes/{scope['id']}")
    for mapper in admin("GET", cpath + "/protocol-mappers/models"):
        admin("DELETE", cpath + "/protocol-mappers/models/" + mapper["id"])
    admin("POST", cpath + "/protocol-mappers/models", {
        "name": "provisioning-query-role", "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-client-role-mapper", "config": {
            "usermodel.clientRoleMapping.clientId": "realm-management",
            "claim.name": "resource_access.realm-management.roles", "multivalued": "true",
            "jsonType.label": "String", "access.token.claim": "true", "id.token.claim": "false",
        },
    })
    sa = admin("GET", cpath + "/service-account-user")["id"]
    while memberships := admin("GET", f"/users/{sa}/groups?first=0&max=100"):
        for membership in memberships:
            admin("DELETE", f"/users/{sa}/groups/{membership['id']}")
    # Repair direct role drift, including composites, rather than leaving an
    # old manage-users assignment which would bypass the group policy.
    for root in (f"/users/{sa}/role-mappings", cpath + "/scope-mappings"):
        mappings = admin("GET", root)
        if mappings.get("realmMappings"):
            admin("DELETE", root + "/realm", mappings["realmMappings"])
        for client in mappings.get("clientMappings", {}).values():
            admin("DELETE", root + "/clients/" + client["id"], client["mappings"])
    query = admin("GET", f"/clients/{management_id}/roles/query-users")
    if query.get("composite"):
        raise RuntimeError("query-users must not be a composite role")
    for root in (f"/users/{sa}/role-mappings", cpath + "/scope-mappings"):
        admin("POST", root + "/clients/" + management_id, [query])

    authz = f"/clients/{_client(admin, 'admin-permissions')['id']}/authz/resource-server"
    _check_owned_policies(admin)
    definitions = [
        ("policy/user", {"name": POLICY_NAME, "logic": "POSITIVE", "users": [sa]}),
        ("permission/scope", {"name": PERMISSION_NAME, "logic": "POSITIVE",
            "resourceType": "Groups", "resources": [group["id"]],
            "scopes": ["view", "view-members", "manage-members", "manage-membership"],
            "policies": [POLICY_NAME], "decisionStrategy": "UNANIMOUS"}),
    ]
    for kind, definition in definitions:
        matches = [p for p in admin("GET", authz + "/" + kind) if p["name"] == definition["name"]]
        if matches:
            admin("PUT", authz + "/" + kind + "/" + matches[0]["id"], definition)
        else:
            admin("POST", authz + "/" + kind, definition)
    admin("PUT", cpath, {"enabled": True})
