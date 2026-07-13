# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Auth infrastructure — permissions and user directory management.

Token signing/verification moved to precis_mcp.oidc (Keycloak RS256 via
JWKS).  This module keeps the things that don't depend on the auth
transport: permissions resolution, the scope evaluator, the user
directory, and the AuthError / AccountDisabledError exception types.
"""

from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, field
from pathlib import Path

from precis_mcp.auth_mode import IDENTITY_COLUMN_ALLOWLIST
from precis_mcp.db import query_clickhouse, query_platform


class AuthError(Exception):
    """Raised on any authentication or authorisation failure."""


class AccountDisabledError(AuthError):
    """Raised when the user's account is flagged is_disabled."""


# ---------------------------------------------------------------------------
# Permissions — role assignments + scope resolution (PostgreSQL-backed)
# ---------------------------------------------------------------------------

# Role hierarchy — higher index = higher privilege.
ROLE_RANK: dict[str, int] = {"analyst": 0, "planner": 1, "manager": 2}

# Tool types and the minimum role rank required to contribute scope.
TOOL_TYPE_MIN_RANK: dict[str, int] = {
    "read": 0,          # analyst+  (all scenario-scoped roles)
    "write": 1,         # planner+
    "plan_manager": 2,  # manager only
    # "admin" is handled separately via is_admin — not scenario-scoped.
    # "general" is the implicit default — no role/scope check.
}


# ── Scope data model ────────────────────────────────────────────────────────
#
# Profile-based model (see deployment/security-model.md):
# allow and deny may coexist on the same axis. Evaluator semantics:
#   effective_set = (allow OR universe) \ deny     — deny always wins.
#
# Per-axis fields:
#   None           → unconstrained on that side (no allow cap / no deny carve-out).
#   empty dict/[]  → explicit empty set (distinguish from None if the YAML says so).


@dataclass(frozen=True)
class DimensionScope:
    """Scope filter on the dimension axis — allow and deny may coexist.

    allow / deny are mappings {dimension_name: [member_ids]}.
    None on either side means "no restriction on that side".
    """
    allow: dict[str, list[str]] | None = None
    deny:  dict[str, list[str]] | None = None


@dataclass(frozen=True)
class DomainScope:
    """Scope filter on the domain axis — allow and deny may coexist."""
    allow: list[str] | None = None
    deny:  list[str] | None = None


@dataclass(frozen=True)
class ScopeSpec:
    """Two independent axes. None on an axis = unrestricted on that axis."""
    dimensions: DimensionScope | None = None
    domains:    DomainScope    | None = None


# ── Pre-computed per-scenario permissions ────────────────────────────────────


@dataclass
class ScenarioPermissions:
    """Effective permissions for one (user, scenario) pair.

    Each user holds at most one profile, so each tool type maps to a single
    scope (or None = unrestricted). A missing key means the user's effective
    role is below the minimum for that tool type.
    """
    effective_role: str
    tool_scopes: dict[str, ScopeSpec | None] = field(default_factory=dict)


@dataclass
class UserPermissions:
    """Full auth context for a user, loaded once per request."""
    user_id: str
    is_admin: bool
    scenarios: dict[str, ScenarioPermissions] = field(default_factory=dict)


# ── Parsing & computation ────────────────────────────────────────────────────


# Category tokens recognised in profile scenario patterns. Mapped to the
# `kind` column on semantic.scenarios.
CATEGORY_TOKENS: dict[str, frozenset[str]] = {
    "ACTUAL":   frozenset({"ACTUAL"}),
    "BUDGET":   frozenset({"BUDGET"}),
    "FORECAST": frozenset({"FORECAST"}),
    "PLAN":     frozenset({"BUDGET", "FORECAST"}),
}

# Specificity ranks — higher wins. Literal id > fine category > PLAN > wildcard.
_PATTERN_RANK_LITERAL       = 4
_PATTERN_RANK_FINE_CATEGORY = 3
_PATTERN_RANK_PLAN          = 2
_PATTERN_RANK_WILDCARD      = 1


def _parse_scope(block: dict | None) -> ScopeSpec | None:
    """Parse one role block from a profile definition into a ScopeSpec.

    Block shape (any field may be omitted):
      {"domains":    {"allow": [...], "deny": [...]},
       "dimensions": {"allow": {dim: [...]}, "deny": {dim: [...]}}}

    Returns None if the block is absent, empty, or has no allow/deny clauses —
    semantically "no restriction at this role level".
    """
    if not block:
        return None
    doms_raw = block.get("domains")
    dims_raw = block.get("dimensions")

    domains: DomainScope | None = None
    if doms_raw:
        allow = doms_raw.get("allow")
        deny  = doms_raw.get("deny")
        if allow is not None or deny is not None:
            domains = DomainScope(allow=allow, deny=deny)

    dimensions: DimensionScope | None = None
    if dims_raw:
        allow_d = dims_raw.get("allow")
        deny_d  = dims_raw.get("deny")
        if allow_d is not None or deny_d is not None:
            dimensions = DimensionScope(allow=allow_d, deny=deny_d)

    if domains is None and dimensions is None:
        return None
    return ScopeSpec(dimensions=dimensions, domains=domains)


def _pattern_match_rank(
    pattern: str, scenario_id: str, scenario_kind: str,
) -> int | None:
    """Return specificity rank if the pattern matches this scenario, else None.

    Precedence: literal id > fine category (BUDGET/FORECAST/ACTUAL) > PLAN > *.
    Unknown tokens return None (validator's responsibility to reject them at write).
    """
    if pattern == scenario_id:
        return _PATTERN_RANK_LITERAL
    if pattern == "*":
        return _PATTERN_RANK_WILDCARD
    members = CATEGORY_TOKENS.get(pattern)
    if members is None or scenario_kind not in members:
        return None
    return _PATTERN_RANK_PLAN if pattern == "PLAN" else _PATTERN_RANK_FINE_CATEGORY


def _resolve_profile_for_scenario(
    profile_def: dict, scenario_id: str, scenario_kind: str,
) -> dict | None:
    """Pick the most-specific pattern entry for this scenario. No merge.

    Returns the matched {role_name: role_block} dict, or None if no pattern
    in the profile matches.
    """
    best_rank = -1
    best_entry: dict | None = None
    for pattern, entry in (profile_def.get("scenarios") or {}).items():
        rank = _pattern_match_rank(pattern, scenario_id, scenario_kind)
        if rank is None or rank <= best_rank:
            continue
        best_rank = rank
        best_entry = entry
    return best_entry


def _resolve_profile_blocks(
    profile_def: dict, scenario_id: str, scenario_kind: str,
) -> dict[str, ScopeSpec | None] | None:
    """Resolve a profile to a {role: scope} dict for this scenario.

    Returns None if no pattern in the profile matches. Unknown role names
    are silently dropped (validator's responsibility to reject at write).
    """
    entry = _resolve_profile_for_scenario(profile_def, scenario_id, scenario_kind)
    if entry is None:
        return None
    blocks: dict[str, ScopeSpec | None] = {}
    for role, block in entry.items():
        if role not in ROLE_RANK:
            continue
        blocks[role] = _parse_scope(block)
    return blocks or None


def _compute_tool_scopes(
    blocks: dict[str, ScopeSpec | None],
) -> tuple[str, dict[str, ScopeSpec | None]]:
    """Compute effective role and per-tool-type scopes from one profile's blocks.

    For a tool requiring min_rank m, pick the block whose role rank is the
    LOWEST declared at or above m. Declaring a role explicitly pins its
    scope for tools at that level — a planner block on the same scenario
    as a manager block means planner-level tools use the planner scope,
    NOT the union. When a level is absent, the selector falls UP to the
    nearest higher declared block (hierarchy fallback).
    """
    if not blocks:
        return "analyst", {}

    effective_role = max(blocks, key=lambda r: ROLE_RANK[r])
    effective_rank = ROLE_RANK[effective_role]

    tool_scopes: dict[str, ScopeSpec | None] = {}

    for tool_type, min_rank in TOOL_TYPE_MIN_RANK.items():
        if effective_rank < min_rank:
            continue
        candidates = [r for r in blocks if ROLE_RANK[r] >= min_rank]
        if not candidates:
            continue
        chosen = min(candidates, key=lambda r: ROLE_RANK[r])
        tool_scopes[tool_type] = blocks[chosen]

    return effective_role, tool_scopes


def _load_scenario_kinds() -> dict[str, str]:
    """Fetch {scenario_id: kind} from semantic.scenarios (ClickHouse)."""
    rows = query_clickhouse(
        "SELECT scenario_id, kind FROM semantic.scenarios",
    )
    return {r["scenario_id"]: r["kind"] for r in rows if r.get("kind")}


def resolve_user_id(claims: dict) -> str | None:
    """Resolve the platform ``user_id`` from verified token claims (gap 2).

    Two optional env knobs; the defaults preserve the shipped behavior:

    - ``PRECIS_IDENTITY_CLAIM`` — which claim carries identity. Unset → the
      shipped behavior (``precis_user_id`` with a ``preferred_username``
      fallback). Set → read exactly that claim, no fallback.
    - ``PRECIS_IDENTITY_COLUMN`` — how the claim value maps to ``users``. ``id``
      (default) → the value *is* the user_id. ``external_id`` (allow-listed,
      unique) → ``SELECT id FROM users WHERE external_id = <value>``, for IdPs
      that emit a stable identifier differing from the Précis user_id.

    Returns the resolved ``user_id``, or ``None`` when the claim is absent or no
    matching user exists (caller rejects with 403). Raises ``AuthError`` if
    ``PRECIS_IDENTITY_COLUMN`` is not allow-listed.
    """
    claim_name = os.environ.get("PRECIS_IDENTITY_CLAIM", "").strip()
    if claim_name:
        value = claims.get(claim_name)
    else:
        value = claims.get("precis_user_id") or claims.get("preferred_username")
    if not value:
        return None

    column = os.environ.get("PRECIS_IDENTITY_COLUMN", "id").strip() or "id"
    if column == "id":
        return value
    if column not in IDENTITY_COLUMN_ALLOWLIST:
        raise AuthError(
            f"PRECIS_IDENTITY_COLUMN={column!r} is not an allowed identity column "
            f"({sorted(IDENTITY_COLUMN_ALLOWLIST)})"
        )
    # `column` is from the allow-list (never request input) → safe to
    # interpolate; the claim value is parameterized.
    rows = query_platform(
        f"SELECT id FROM users WHERE {column} = %s",  # noqa: S608 — allow-listed column
        (value,),
    )
    return rows[0]["id"] if rows else None


def load_permissions(user_id: str) -> UserPermissions:
    """Load user context and profile-driven scopes, pre-compute tool scopes.

    Pipeline:
      1. users — is_admin.
      2. user_profile_assignments ⋈ profiles — active (non-expired) profile
         definitions for this user.
      3. semantic.scenarios — (scenario_id, kind) lookup for pattern matching.
      4. For each scenario: resolve the most-specific pattern per profile,
         expand role blocks, union across profiles (list concatenation),
         then run the existing role-hierarchy resolver (_compute_tool_scopes).

    Scenario-pattern precedence: literal id > fine category (BUDGET /
    FORECAST / ACTUAL) > PLAN > *. Most specific wins, no merge within a
    profile. Multi-profile union is additive (any profile may grant).

    Raises:
        AuthError: If the user is not found.
    """
    # 1. User row
    user_rows = query_platform(
        "SELECT is_admin, is_disabled FROM users WHERE id = %s",
        (user_id,),
    )
    if not user_rows:
        raise AuthError(f"User '{user_id}' not found")
    user_row = user_rows[0]
    if bool(user_row.get("is_disabled", False)):
        raise AccountDisabledError(f"Account '{user_id}' is disabled")

    # 2. Active profile definition (one per user, by policy)
    profile_rows = query_platform(
        "SELECT p.definition "
        "FROM user_profile_assignments upa "
        "JOIN profiles p ON p.profile_id = upa.profile_id "
        "WHERE upa.user_id = %s "
        "  AND (upa.expires_at IS NULL OR upa.expires_at > now())",
        (user_id,),
    )
    profile_def = profile_rows[0]["definition"] if profile_rows else None

    # 3. Scenario kinds (from ClickHouse — source of truth for scenarios)
    scenario_kinds = _load_scenario_kinds() if profile_def else {}

    # 4. Per-scenario resolution
    scenarios: dict[str, ScenarioPermissions] = {}
    for sid, kind in scenario_kinds.items():
        if profile_def is None:
            break
        blocks = _resolve_profile_blocks(profile_def, sid, kind)
        if not blocks:
            continue
        effective_role, tool_scopes = _compute_tool_scopes(blocks)
        scenarios[sid] = ScenarioPermissions(
            effective_role=effective_role,
            tool_scopes=tool_scopes,
        )

    return UserPermissions(
        user_id=user_id,
        is_admin=bool(user_row.get("is_admin", False)),
        scenarios=scenarios,
    )


# ---------------------------------------------------------------------------
# User directory (filesystem layout for the registry-backed file store)
# ---------------------------------------------------------------------------

def _user_data_base() -> str:
    return os.getenv("USER_DATA_DIR", "/data/users")


# Sandbox-runner uid — see Dockerfile.sandbox-runner (USER runner, uid 10001)
# and docker/Dockerfile.sandbox (USER sandbox, uid 10001). The api container
# runs as root; dirs it creates under USER_DATA_DIR are root-owned and
# unwritable by the runner. Chown after mkdir so the runner can copy
# outputs back. Best-effort: chown requires CAP_CHOWN; in dev the api may
# run unprivileged, in which case we skip and rely on matching uids in
# that environment.
_SANDBOX_UID = int(os.getenv("SANDBOX_USER_UID", "10001"))


def chown_sandbox(path: Path) -> None:
    try:
        os.chown(path, _SANDBOX_UID, _SANDBOX_UID)
    except PermissionError:
        pass
    except OSError:
        pass


def ensure_user_directory(user_id: str) -> str:
    """Create the user file storage directories if they do not exist.

    Layout (post-2026-04-30 registry migration):
        <USER_DATA_DIR>/<user_id>/blobs/             — file content (file_id-keyed)
        <USER_DATA_DIR>/<user_id>/sandbox_outputs/   — per-run sandbox staging,
                                                       picked up by the API for
                                                       registration
        <USER_DATA_DIR>/<user_id>/sandbox_staging/   — per-call input staging
                                                       (data-ref payloads the
                                                       API materialises for the
                                                       runner)

    All metadata (credentials, permissions, profile, workstreams, memories,
    tasks, files registry) lives in PostgreSQL.

    The chown ensures the sandbox runner (uid 10001) can read user-uploaded
    blobs and write outputs into the staging dir on the shared user_data
    volume.
    """
    base = Path(_user_data_base()) / user_id
    for subdir in ("", "blobs", "sandbox_outputs", "sandbox_staging"):
        p = base / subdir
        p.mkdir(parents=True, exist_ok=True)
        chown_sandbox(p)
    return str(base)


# ---------------------------------------------------------------------------
# Request-scoped auth context (set per-request by JWTAuthMiddleware via a
# contextvar; read by every tool running within the same request)
# ---------------------------------------------------------------------------


@dataclass
class AuthContext:
    """Authenticated user context, set per-request by JWT middleware."""

    user_id: str
    permissions: UserPermissions
    conversation_id: str = ""
    # Report-builder context — set by the Précis agent when the
    # report panel is open; unused by the open read surface.
    active_report_id: str = ""
    active_block_id: str = ""


_auth_context: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "_auth_context", default=None
)


def set_auth_context(ctx: AuthContext) -> None:
    _auth_context.set(ctx)


def get_auth_context() -> AuthContext:
    ctx = _auth_context.get()
    if ctx is None:
        raise RuntimeError("No auth context set — is JWTAuthMiddleware active?")
    return ctx


def clear_auth_context() -> None:
    _auth_context.set(None)


def require_user_id(value, tool_name: str) -> None:
    """Fail loudly if ``user_id`` is missing or falsy at write-tool entry.

    Write tools take ``user_id`` as a wrapper-injected keyword-only parameter.
    If injection silently fails (broken auth context, dev MCP path without an
    explicit user_id, a test fixture leaking into prod), the write must NOT
    proceed under a fallback identity — mis-attribution of financial writes is
    unrecoverable.
    """
    if not value:
        raise PermissionError(
            f"{tool_name}: user_id is required and must be supplied via "
            "AuthContext (agent path) or explicitly (dev MCP path)."
        )


# ---------------------------------------------------------------------------
# Per-call scope (set by the permission gate, injected by the tool wrapper)
# ---------------------------------------------------------------------------


_call_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_call_scope", default=None
)


def set_call_scope(scope: dict | None) -> None:
    _call_scope.set(scope)


def get_call_scope() -> dict | None:
    return _call_scope.get()


def clear_call_scope() -> None:
    _call_scope.set(None)
