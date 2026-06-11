# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Admin operations core — schemas, validation, and platform-object CRUD.

Open-core module shared by both admin surfaces: the commercial HTTP admin
routes (`precis/routes/admin.py`) and the open admin CLI
(`precis_mcp/admin_cli.py`). It owns the Pydantic schemas (profile tree + input
bodies), the schema registry the admin UI renders, and — in a later increment —
the user/profile/assignment write operations.

The logic raises the domain exceptions below, never `HTTPException`: the HTTP
layer maps them to status codes, the CLI maps them to exit codes. This keeps the
core free of any web-framework dependency so it is reusable from a headless CLI.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal, Optional

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# Assignment provenance values the assignment-source CHECK accepts (open/003
# adds ``admin_cli`` for CLI-driven grants).
VALID_ASSIGNMENT_SOURCES = {"seed", "admin_ui", "api", "sso_sync", "admin_cli"}


# ---------------------------------------------------------------------------
# Domain exceptions — surface-agnostic. HTTP maps to status; CLI maps to exit.
# ---------------------------------------------------------------------------


class AdminError(Exception):
    """Base for admin-operation failures."""


class NotFoundError(AdminError):
    """A referenced user/profile/assignment does not exist (HTTP 404)."""


class ConflictError(AdminError):
    """The object already exists or is still referenced (HTTP 409)."""


class AdminValidationError(AdminError):
    """Input failed validation (HTTP 422)."""


class ProvisioningError(AdminError):
    """An upstream identity-provider (Keycloak) call failed (HTTP 502)."""


# Status / exit mapping, single source so both surfaces agree.
ADMIN_HTTP_STATUS: dict[type[AdminError], int] = {
    NotFoundError: 404,
    ConflictError: 409,
    AdminValidationError: 422,
    ProvisioningError: 502,
}

ADMIN_EXIT_CODE: dict[type[AdminError], int] = {
    AdminValidationError: 2,
    NotFoundError: 4,
    ConflictError: 5,
    ProvisioningError: 6,
}


# ---------------------------------------------------------------------------
# Request bodies (shared by HTTP routes and the CLI's structured inputs)
# ---------------------------------------------------------------------------


class CreateUserBody(BaseModel):
    id: str
    password: str
    name: str = ""
    is_admin: bool = False


class PatchUserBody(BaseModel):
    is_admin: bool | None = None
    is_disabled: bool | None = None
    identity: dict | None = None


class ResetPasswordBody(BaseModel):
    password: str


class AssignProfileBody(BaseModel):
    profile_id: str
    source: str = "admin_ui"


class ProfileYamlBody(BaseModel):
    """Profile create/update payload — accepts either raw YAML (legacy /
    YAML-editor path) or a structured `data` dict (schema-driven form path).
    Exactly one of `yaml` / `data` must be provided."""

    yaml: str | None = None
    data: dict | None = None
    change_reason: str | None = None


# ---------------------------------------------------------------------------
# Profile schema — full Pydantic model of the scenarios/roles/permissions tree.
# Same model is used for runtime validation (via `parse_profile_yaml`) and JSON
# Schema generation (the admin UI consumes it via /api/admin/schemas).
# ---------------------------------------------------------------------------

Role = Literal["analyst", "planner", "manager"]


class DomainScope(BaseModel):
    """Domain-axis scope: explicit allow list, explicit deny list, or both.
    Empty allow means "universe"; deny subtracts members."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class DimensionScope(BaseModel):
    """Dimension-axis scope. Keys are dimension names (e.g. 'cost_centre',
    'department'); values are lists of member ids. Multiple keys combine
    as AND across the scope clauses; deny-wins on conflict."""

    model_config = ConfigDict(extra="forbid")

    allow: dict[str, list[str]] = Field(default_factory=dict)
    deny: dict[str, list[str]] = Field(default_factory=dict)


class RoleScope(BaseModel):
    """Per-role scope block. Both fields optional — an empty block means
    'full access at this role level'."""

    model_config = ConfigDict(extra="forbid")

    domains: Optional[DomainScope] = None
    dimensions: Optional[DimensionScope] = None


def _none_to_empty(v: Any) -> Any:
    """Coerce YAML null role bodies (`manager:` with no body) to `{}` so the
    field stays a plain RoleScope. Keeps the generated JSON Schema free of
    anyOf/null unions (cleaner admin form)."""
    return {} if v is None else v


RoleScopeOrEmpty = Annotated[RoleScope, BeforeValidator(_none_to_empty)]


class ScenarioBlock(BaseModel):
    """Three named optional role slots for one scenario pattern.

    Each slot may be absent (role not granted at this level) or present
    (granted, with optional scope restrictions; empty `RoleScope()` =
    full access). Modelled as named fields rather than a `dict[Role, ...]`
    so the admin form can render three known slots with a toggle each,
    not a free-text key editor.

    Explicit field `title=` overrides the resolved `$ref` title so the
    admin form shows 'Analyst' / 'Planner' / 'Manager' (the role name)
    instead of 'RoleScope' three times."""

    model_config = ConfigDict(extra="forbid")

    analyst: Optional[RoleScopeOrEmpty] = Field(default=None, title="Analyst")
    planner: Optional[RoleScopeOrEmpty] = Field(default=None, title="Planner")
    manager: Optional[RoleScopeOrEmpty] = Field(default=None, title="Manager")


class Profile(BaseModel):
    """Security profile — scenarios pattern → role → scope tree.

    `scenarios` keys are scenario patterns (literal scenario id, category
    token like 'PLAN', or '*')."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")
    name: str = Field(min_length=1)
    description: str = ""
    scenarios: dict[str, ScenarioBlock]


# ---------------------------------------------------------------------------
# Admin schema registry — single source for the admin UI's schema-driven forms.
# ---------------------------------------------------------------------------


def admin_schema_registry() -> dict[str, type[BaseModel]]:
    """Map of {name → Pydantic model} the admin UI may render as a form."""
    from precis_mcp.ingestion.registry import Binding, Source

    return {
        "binding": Binding,
        "source": Source,
        "profile": Profile,
    }


def strip_null_from_anyof(node: Any) -> Any:
    """Walk a JSON Schema and collapse `{anyOf: [X, {type: null}]}` → `X`.

    Pydantic emits this shape for every `Optional[X]` field. RJSF then
    renders a variant-picker dropdown ('X / Option 2'), which is noise —
    optionality is already expressible by leaving the field out of the
    parent's `required` list. After this pass, RJSF treats the field as
    a plain optional `X` and renders the theme's optional-data toggle
    instead."""
    if isinstance(node, dict):
        if "anyOf" in node and isinstance(node["anyOf"], list):
            non_null = [
                s for s in node["anyOf"]
                if not (isinstance(s, dict) and s.get("type") == "null")
            ]
            if len(non_null) == 1 and len(non_null) < len(node["anyOf"]):
                preserved = {k: v for k, v in node.items() if k != "anyOf"}
                merged = {**non_null[0], **preserved}
                return strip_null_from_anyof(merged)
        return {k: strip_null_from_anyof(v) for k, v in node.items()}
    if isinstance(node, list):
        return [strip_null_from_anyof(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# Validation + profile parsing/rendering helpers
# ---------------------------------------------------------------------------


def validate_password(plain: str) -> None:
    """Enforce the minimum password length. Raises AdminValidationError."""
    if len(plain) < 12:
        raise AdminValidationError("Password must be at least 12 characters")


def parse_profile_yaml(raw: str) -> tuple[str, str, str, dict]:
    """Parse a full profile YAML payload via the Pydantic Profile model.

    Returns (profile_id, name, description, definition). The definition
    contains only the scenarios tree (profile_id/name/description live in
    their own columns)."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AdminValidationError(f"Invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AdminValidationError("Profile YAML must be a mapping")

    try:
        profile = Profile.model_validate(parsed)
    except ValidationError as exc:
        raise AdminValidationError(str(exc)) from exc

    definition = {
        "scenarios": profile.model_dump(exclude_none=True, mode="json")["scenarios"],
    }
    return profile.profile_id, profile.name.strip(), profile.description, definition


def parse_profile_body(body: ProfileYamlBody) -> tuple[str, str, str, dict]:
    """Dispatch a profile payload to the right parser (yaml or data)."""
    raw_yaml = body.yaml
    raw_data = body.data
    if raw_yaml is not None and raw_data is not None:
        raise AdminValidationError("Provide either 'yaml' or 'data', not both")
    if raw_yaml is None and raw_data is None:
        raise AdminValidationError("Provide 'yaml' (string) or 'data' (object)")
    if raw_yaml is not None:
        return parse_profile_yaml(raw_yaml)
    try:
        profile = Profile.model_validate(raw_data)
    except ValidationError as exc:
        raise AdminValidationError(str(exc)) from exc
    definition = {
        "scenarios": profile.model_dump(exclude_none=True, mode="json")["scenarios"],
    }
    return profile.profile_id, profile.name.strip(), profile.description, definition


def profile_yaml_repr(row: dict) -> str:
    """Render a profile row as a full YAML document (round-trip input)."""
    body: dict[str, Any] = {
        "profile_id": row["profile_id"],
        "name": row["name"],
        "description": row.get("description") or "",
    }
    definition = row.get("definition") or {}
    if isinstance(definition, str):
        definition = json.loads(definition)
    body.update(definition)
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Platform-object operations — shared by the HTTP admin routes and the CLI.
#
# They operate on the OPEN grain (users / profiles / assignments + Keycloak).
# The commercial identity grain (user_profile_ext) is layered by the route via
# the ``on_db_insert`` hook; the CLI passes none. db/auth/keycloak imports are
# function-local so importing this module (for its schemas) pulls no I/O deps.
# ---------------------------------------------------------------------------


def _require_user(user_id: str) -> None:
    from precis_mcp.db import query_platform

    if not query_platform("SELECT id FROM users WHERE id = %s", (user_id,)):
        raise NotFoundError(f"User '{user_id}' not found")


def create_user(
    *,
    id: str,
    actor: str,
    password: str | None = None,
    is_admin: bool = False,
    name: str = "",
    external_id: str | None = None,
    provision_keycloak: bool = True,
    on_db_insert: Callable[[], None] | None = None,
) -> None:
    """Create a platform user (open grain) and, in mode B, the Keycloak account.

    ``provision_keycloak=False`` (mode C) creates only the platform row — the
    external IdP owns the credential, so no password is required. ``on_db_insert``
    runs inside the same rollback envelope as the users insert, so a commercial
    caller can write its identity grain and have a failure roll back Keycloak too.
    """
    from precis_mcp.auth import ensure_user_directory
    from precis_mcp.db import execute_platform, query_platform
    from precis_mcp.keycloak_admin import KeycloakAdmin, KeycloakAdminError

    # Validate the password before any DB read (matches the original route
    # ordering: weak password → 422 regardless of whether the id exists).
    if provision_keycloak:
        if not password:
            raise AdminValidationError(
                "password is required when provisioning Keycloak"
            )
        validate_password(password)

    if query_platform("SELECT id FROM users WHERE id = %s", (id,)):
        raise ConflictError(f"User '{id}' already exists")

    kc: Any = None
    if provision_keycloak:
        kc = KeycloakAdmin()
        try:
            kc.create_user(
                username=id, precis_user_id=id, password=password,
                name=name, temporary=True,
            )
        except KeycloakAdminError as exc:
            raise ProvisioningError(str(exc)) from exc

    try:
        if external_id is not None:
            execute_platform(
                "INSERT INTO users (id, is_admin, external_id) VALUES (%s, %s, %s)",
                (id, is_admin, external_id),
            )
        else:
            execute_platform("INSERT INTO users (id, is_admin) VALUES (%s, %s)", (id, is_admin))
        if on_db_insert is not None:
            on_db_insert()
        ensure_user_directory(id)
    except Exception:
        if kc is not None:
            try:
                kc.delete_user(id)
            except KeycloakAdminError:
                logger.exception(
                    "Failed to roll back Keycloak user %s after DB insert failed", id
                )
        raise


def set_admin(*, user_id: str, is_admin: bool, actor: str) -> None:
    from precis_mcp.db import execute_platform

    _require_user(user_id)
    execute_platform(
        "UPDATE users SET is_admin = %s, updated_at = now() WHERE id = %s",
        (is_admin, user_id),
    )


def disable_user(*, user_id: str, actor: str, self_id: str | None = None) -> None:
    from precis_mcp.db import execute_platform
    from precis_mcp.keycloak_admin import KeycloakAdmin, KeycloakAdminError

    if self_id is not None and user_id == self_id:
        raise AdminValidationError("Cannot disable your own account")
    _require_user(user_id)
    try:
        KeycloakAdmin().set_enabled(username=user_id, enabled=False)
    except KeycloakAdminError as exc:
        logger.warning("Keycloak disable failed for %s: %s", user_id, exc)
    execute_platform(
        "UPDATE users SET is_disabled = true, updated_at = now() WHERE id = %s",
        (user_id,),
    )


def reset_password(*, user_id: str, password: str, actor: str) -> None:
    from precis_mcp.keycloak_admin import KeycloakAdmin, KeycloakAdminError

    validate_password(password)
    _require_user(user_id)
    try:
        KeycloakAdmin().reset_password(
            username=user_id, password=password, temporary=True,
        )
    except KeycloakAdminError as exc:
        raise ProvisioningError(str(exc)) from exc


def create_profile(*, body: ProfileYamlBody, actor: str) -> str:
    from precis_mcp.db import execute_platform, query_platform

    profile_id, name, description, definition = parse_profile_body(body)
    if query_platform("SELECT profile_id FROM profiles WHERE profile_id = %s", (profile_id,)):
        raise ConflictError(f"Profile '{profile_id}' already exists")
    execute_platform(
        "INSERT INTO profiles (profile_id, name, description, definition, updated_by) "
        "VALUES (%s, %s, %s, %s::jsonb, %s)",
        (profile_id, name, description, json.dumps(definition), actor),
    )
    execute_platform(
        "INSERT INTO profile_audit "
        "(profile_id, definition, changed_by, change_reason, change_kind) "
        "VALUES (%s, %s::jsonb, %s, %s, 'create')",
        (profile_id, json.dumps(definition), actor, body.change_reason),
    )
    return profile_id


def update_profile(*, profile_id: str, body: ProfileYamlBody, actor: str) -> None:
    from precis_mcp.db import execute_platform, query_platform

    if not query_platform("SELECT profile_id FROM profiles WHERE profile_id = %s", (profile_id,)):
        raise NotFoundError(f"Profile '{profile_id}' not found")
    payload_pid, name, description, definition = parse_profile_body(body)
    if payload_pid != profile_id:
        raise AdminValidationError(
            f"profile_id in payload ('{payload_pid}') does not match "
            f"target ('{profile_id}')"
        )
    execute_platform(
        "UPDATE profiles SET name = %s, description = %s, definition = %s::jsonb, "
        "updated_by = %s, updated_at = now() WHERE profile_id = %s",
        (name, description, json.dumps(definition), actor, profile_id),
    )
    execute_platform(
        "INSERT INTO profile_audit "
        "(profile_id, definition, changed_by, change_reason, change_kind) "
        "VALUES (%s, %s::jsonb, %s, %s, 'update')",
        (profile_id, json.dumps(definition), actor, body.change_reason),
    )


def delete_profile(*, profile_id: str, actor: str) -> None:
    from precis_mcp.db import execute_platform, query_platform

    rows = query_platform("SELECT definition FROM profiles WHERE profile_id = %s", (profile_id,))
    if not rows:
        raise NotFoundError(f"Profile '{profile_id}' not found")
    in_use = query_platform(
        "SELECT user_id FROM user_profile_assignments WHERE profile_id = %s", (profile_id,)
    )
    if in_use:
        raise ConflictError(
            f"Profile '{profile_id}' is assigned to {len(in_use)} user(s); "
            "revoke assignments first"
        )
    definition = rows[0]["definition"]
    definition_json = definition if isinstance(definition, str) else json.dumps(definition)
    execute_platform(
        "INSERT INTO profile_audit (profile_id, definition, changed_by, change_kind) "
        "VALUES (%s, %s::jsonb, %s, 'delete')",
        (profile_id, definition_json, actor),
    )
    execute_platform("DELETE FROM profiles WHERE profile_id = %s", (profile_id,))


def assign_profile(*, user_id: str, profile_id: str, actor: str, source: str = "admin_ui") -> None:
    from precis_mcp.db import execute_platform, query_platform

    _require_user(user_id)
    if source not in VALID_ASSIGNMENT_SOURCES:
        raise AdminValidationError(
            f"source must be one of {sorted(VALID_ASSIGNMENT_SOURCES)}"
        )
    if not query_platform("SELECT profile_id FROM profiles WHERE profile_id = %s", (profile_id,)):
        raise NotFoundError(f"Profile '{profile_id}' not found")
    execute_platform(
        "INSERT INTO user_profile_assignments "
        "(user_id, profile_id, granted_by, source) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET profile_id = EXCLUDED.profile_id, "
        "granted_by = EXCLUDED.granted_by, granted_at = now(), source = EXCLUDED.source",
        (user_id, profile_id, actor, source),
    )


def revoke_profile(*, user_id: str, actor: str) -> str:
    from precis_mcp.db import execute_platform, query_platform

    _require_user(user_id)
    existing = query_platform(
        "SELECT profile_id FROM user_profile_assignments WHERE user_id = %s", (user_id,)
    )
    if not existing:
        raise NotFoundError("No profile assigned")
    execute_platform("DELETE FROM user_profile_assignments WHERE user_id = %s", (user_id,))
    return existing[0]["profile_id"]


# --- open-grain reads (the CLI's list/show; the commercial route keeps its
#     richer user_full reads) ----------------------------------------------


def list_users() -> list[dict]:
    from precis_mcp.db import query_platform

    return [
        dict(r) for r in query_platform(
            "SELECT id, is_admin, is_disabled, created_at, updated_at "
            "FROM users ORDER BY created_at DESC"
        )
    ]


def get_user(user_id: str) -> dict:
    from precis_mcp.db import query_platform

    rows = query_platform(
        "SELECT id, is_admin, is_disabled, created_at, updated_at "
        "FROM users WHERE id = %s",
        (user_id,),
    )
    if not rows:
        raise NotFoundError(f"User '{user_id}' not found")
    return dict(rows[0])


def list_profiles() -> list[dict]:
    from precis_mcp.db import query_platform

    return [
        dict(r) for r in query_platform(
            "SELECT profile_id, name, description, definition, updated_by, updated_at "
            "FROM profiles ORDER BY profile_id"
        )
    ]


def get_profile(profile_id: str) -> dict:
    from precis_mcp.db import query_platform

    rows = query_platform(
        "SELECT profile_id, name, description, definition, updated_by, updated_at "
        "FROM profiles WHERE profile_id = %s",
        (profile_id,),
    )
    if not rows:
        raise NotFoundError(f"Profile '{profile_id}' not found")
    return dict(rows[0])


def get_user_profile(user_id: str) -> dict | None:
    from precis_mcp.db import query_platform

    rows = query_platform(
        "SELECT upa.user_id, upa.profile_id, p.name AS profile_name, upa.granted_by, "
        "upa.granted_at, upa.expires_at, upa.source "
        "FROM user_profile_assignments upa "
        "JOIN profiles p ON p.profile_id = upa.profile_id "
        "WHERE upa.user_id = %s",
        (user_id,),
    )
    return dict(rows[0]) if rows else None
