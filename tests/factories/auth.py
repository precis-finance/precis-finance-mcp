# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Domain-object builders for auth-related tests.

Produces user records, JWT tokens, and the bearer-header
shape that endpoint tests pass into FastAPI `TestClient`. Replaces the
inline `create_token(...)` + `f"Bearer {…}"` + ad-hoc user-dict patterns
duplicated across 6+ endpoint test files.

The user dict shape matches `tests/fakes/fake_platform_db.FakePlatformDB._default_user`
(which mirrors the production `users` table). Override any field via the
keyword args.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = [
    "make_user",
    "make_token",
    "make_auth_headers",
    "make_scenario_permissions",
    "make_permissions",
]


def make_user(
    user_id: str = "testuser",
    *,
    role: str = "analyst",
    is_admin: bool = False,
    is_disabled: bool = False,
    identity: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A user row suitable for `FakePlatformDB.users.append(...)`.

    Field shape matches `FakePlatformDB._default_user`. `overrides` lets a
    test patch a field (e.g. `scope={"cost_centre": ["CC-01"]}`) without
    enumerating the full default.
    """
    now = datetime.now(timezone.utc)
    user = {
        "id": user_id,
        "role": role,
        "scope": {},
        "write_scope": {},
        "scenario_access": {},
        "identity": identity or {},
        "preferences": "",
        "skill_preferences": {},
        "report_context": {},
        "onboarded_at": None,
        "is_admin": is_admin,
        "is_disabled": is_disabled,
        "created_at": now,
        "updated_at": now,
    }
    user.update(overrides)
    return user


def make_token(user_id: str = "testuser") -> str:
    """Sentinel test token the conftest-installed Keycloak verifier
    recognises and decodes into the given user claim.

    Wire format: ``test:<user_id>``.  Not a real signed JWT — no signing
    key, no expiry, no cryptography.  The patched
    ``precis_mcp.oidc.verify_keycloak_token`` in conftest converts it
    into the same claims shape Keycloak would have issued.
    """
    return f"test:{user_id}"


def make_auth_headers(user_id: str = "testuser") -> dict[str, str]:
    """The `Authorization: Bearer …` header dict for `TestClient(...).post(headers=…)`."""
    return {"Authorization": f"Bearer {make_token(user_id)}"}


def make_scenario_permissions(
    effective_role: str = "analyst",
    tool_scopes: dict[str, Any] | None = None,
) -> Any:
    """A `ScenarioPermissions` for one (user, scenario) pair.

    `tool_scopes` defaults to ``{"read": None}`` — read access, unrestricted
    scope. Pass an explicit map (e.g. ``{"write": some_scope_spec}``) to grant
    other tool types or attach a `ScopeSpec`.
    """
    from precis_mcp.auth import ScenarioPermissions

    return ScenarioPermissions(
        effective_role=effective_role,
        tool_scopes={"read": None} if tool_scopes is None else tool_scopes,
    )


def make_permissions(
    user_id: str = "testuser",
    *,
    is_admin: bool = False,
    scenarios: dict[str, Any] | None = None,
) -> Any:
    """A `UserPermissions` auth context — the object `load_permissions`
    returns and that endpoint tests patch `precis.agui.load_permissions`
    with. The middleware reads attributes off it, so patch with this,
    never a raw dict.
    """
    from precis_mcp.auth import UserPermissions

    return UserPermissions(
        user_id=user_id,
        is_admin=is_admin,
        scenarios=scenarios if scenarios is not None else {},
    )
