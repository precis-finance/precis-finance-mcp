# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Integration-style test for the profile resolver helpers in precis_mcp.auth.

Loads every YAML in mock_profiles/ directly (no Postgres), runs the new
resolver pipeline for a hand-picked set of (scenario_id, scenario_kind,
tool-role) probes, and asserts the resulting ScenarioPermissions objects
match the ``# Expect:`` comments in each fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from precis_mcp.auth import (
    CATEGORY_TOKENS,
    DimensionScope,
    DomainScope,
    ROLE_RANK,
    ScenarioPermissions,
    ScopeSpec,
    TOOL_TYPE_MIN_RANK,
    _compute_tool_scopes,
    _parse_scope,
    _pattern_match_rank,
    _resolve_profile_blocks,
    _resolve_profile_for_scenario,
)

# The three fixture scenarios, mirroring the synthetic semantic.scenarios
# rows once the `kind` column is populated.
SCENARIOS: dict[str, str] = {
    "ACTUALS":    "ACTUAL",
    "BUD-2026":   "BUDGET",
    "FC-2026-Q1": "FORECAST",
}

MOCK_DIR = Path(__file__).resolve().parents[2] / "mock_profiles"


def _load(name: str) -> dict:
    """Load one profile YAML into its `definition` dict (scenarios:…)."""
    raw = yaml.safe_load((MOCK_DIR / name).read_text())
    # Strip the metadata fields (profile_id, name, description) — the
    # resolver only ever sees the `scenarios` block at runtime.
    return {"scenarios": raw["scenarios"]}


def _resolve(profile_name: str) -> dict[str, ScenarioPermissions]:
    """Full resolver pipeline for one profile, returning {scenario_id: perms}."""
    pdef = _load(profile_name)
    out: dict[str, ScenarioPermissions] = {}
    for sid, kind in SCENARIOS.items():
        blocks = _resolve_profile_blocks(pdef, sid, kind)
        if not blocks:
            continue
        role, tool_scopes = _compute_tool_scopes(blocks)
        out[sid] = ScenarioPermissions(effective_role=role, tool_scopes=tool_scopes)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Low-level helpers — sanity checks on parse + match ranking
# ────────────────────────────────────────────────────────────────────────────


def test_category_tokens_cover_all_kinds():
    assert CATEGORY_TOKENS["PLAN"] == frozenset({"BUDGET", "FORECAST"})
    assert CATEGORY_TOKENS["ACTUAL"] == frozenset({"ACTUAL"})


def test_pattern_match_rank_precedence():
    # Literal > fine category > PLAN > wildcard
    assert _pattern_match_rank("BUD-2026",   "BUD-2026", "BUDGET") == 4
    assert _pattern_match_rank("BUDGET",     "BUD-2026", "BUDGET") == 3
    assert _pattern_match_rank("PLAN",       "BUD-2026", "BUDGET") == 2
    assert _pattern_match_rank("*",          "BUD-2026", "BUDGET") == 1
    # PLAN does NOT match ACTUAL
    assert _pattern_match_rank("PLAN",       "ACTUALS",  "ACTUAL") is None
    # Unknown token → no match
    assert _pattern_match_rank("PLAN_*",     "BUD-2026", "BUDGET") is None


def test_parse_scope_empty_block_is_none():
    assert _parse_scope(None) is None
    assert _parse_scope({}) is None
    # `manager: {}` — full access at that level
    assert _parse_scope({}) is None


def test_parse_scope_allow_and_deny_coexist():
    block = {
        "domains":    {"allow": ["gl"], "deny": ["payroll"]},
        "dimensions": {"allow": {"department": ["X"]}, "deny": {"cost_centre": ["CC-1"]}},
    }
    scope = _parse_scope(block)
    assert isinstance(scope, ScopeSpec)
    assert scope.domains == DomainScope(allow=["gl"], deny=["payroll"])
    assert scope.dimensions == DimensionScope(
        allow={"department": ["X"]}, deny={"cost_centre": ["CC-1"]}
    )


def test_parse_scope_deny_only():
    scope = _parse_scope({"domains": {"deny": ["payroll"]}})
    assert scope is not None
    assert scope.domains == DomainScope(allow=None, deny=["payroll"])
    assert scope.dimensions is None


# ────────────────────────────────────────────────────────────────────────────
# Per-fixture assertions — one test per profile YAML
# ────────────────────────────────────────────────────────────────────────────


def test_01_cfo_full_access():
    """Wildcard + empty manager block → full access on every scenario,
    including analyst/planner tool calls via hierarchy fallback."""
    perms = _resolve("01_cfo_full_access.yml")
    assert set(perms) == set(SCENARIOS)
    for sid, sp in perms.items():
        assert sp.effective_role == "manager", sid
        # All three tool types permitted, all unrestricted
        assert set(sp.tool_scopes) == set(TOOL_TYPE_MIN_RANK)
        for tool_type, scope in sp.tool_scopes.items():
            assert scope is None, f"{sid}/{tool_type} should be unrestricted"


def test_02_fpa_lead():
    perms = _resolve("02_fpa_lead.yml")
    # PLAN matches BUD-2026 and FC-2026-Q1 with empty manager block
    for sid in ("BUD-2026", "FC-2026-Q1"):
        sp = perms[sid]
        assert sp.effective_role == "manager"
        assert all(sp.tool_scopes[t] is None for t in TOOL_TYPE_MIN_RANK)
    # ACTUALS: analyst only, no payroll domain
    sp = perms["ACTUALS"]
    assert sp.effective_role == "analyst"
    assert "read" in sp.tool_scopes
    assert "write" not in sp.tool_scopes
    assert "plan_manager" not in sp.tool_scopes
    scope = sp.tool_scopes["read"]
    assert scope is not None
    assert scope.domains == DomainScope(allow=None, deny=["payroll"])


def test_03_cloud_planner():
    perms = _resolve("03_cloud_planner.yml")
    # BUD-2026: planner role (scoped), no manager tools available
    sp = perms["BUD-2026"]
    assert sp.effective_role == "planner"
    assert "plan_manager" not in sp.tool_scopes
    assert "write" in sp.tool_scopes and "read" in sp.tool_scopes
    # Scope should restrict department
    for tt in ("read", "write"):
        scope = sp.tool_scopes[tt]
        assert scope is not None
        assert scope.dimensions == DimensionScope(
            allow={"department": ["Cloud & Infrastructure"]}, deny=None
        )
    # ACTUALS analyst-only
    sp = perms["ACTUALS"]
    assert sp.effective_role == "analyst"
    assert "write" not in sp.tool_scopes
    # FC-2026-Q1 not in profile (no category token / no literal / no `*`)
    assert "FC-2026-Q1" not in perms


def test_04_software_eng_manager_per_role_scope():
    """Different scope per role level on the same PLAN pattern.

    Per-profile selection rule: planner-level tools use the planner block's
    scope (NOT planner ∪ manager); manager-level tools use the manager block.
    Declaring a role explicitly pins that level's scope.
    """
    perms = _resolve("04_software_eng_manager.yml")
    for sid in ("BUD-2026", "FC-2026-Q1"):
        sp = perms[sid]
        assert sp.effective_role == "manager"
        # manager-level tool → manager block
        pm = sp.tool_scopes["plan_manager"]
        assert pm is not None
        assert pm.dimensions == DimensionScope(
            allow={"department": ["Software Engineering"]},
            deny={"cost_centre": ["CC-SENG-06"]},
        )
        # planner-level tool → planner block ONLY (not union with manager)
        wr = sp.tool_scopes["write"]
        assert wr is not None
        assert wr.dimensions == DimensionScope(
            allow={"cost_centre": ["CC-SENG-01", "CC-SENG-02", "CC-SENG-03"]},
            deny=None,
        )
        # analyst-level tool → falls UP to the nearest declared level → planner
        rd = sp.tool_scopes["read"]
        assert rd is not None
        assert rd.dimensions == DimensionScope(
            allow={"cost_centre": ["CC-SENG-01", "CC-SENG-02", "CC-SENG-03"]},
            deny=None,
        )
    # ACTUAL analyst block
    sp = perms["ACTUALS"]
    assert sp.effective_role == "analyst"
    rd = sp.tool_scopes["read"]
    assert rd is not None and rd.dimensions is not None
    assert rd.dimensions.allow == {"department": ["Software Engineering"]}


def test_05_external_auditor_literal_only():
    perms = _resolve("05_external_auditor.yml")
    assert set(perms) == set(SCENARIOS)
    for sid, sp in perms.items():
        assert sp.effective_role == "analyst"
        assert "read" in sp.tool_scopes
        assert "write" not in sp.tool_scopes
        assert "plan_manager" not in sp.tool_scopes
        scope = sp.tool_scopes["read"]
        assert scope is not None
        assert scope.domains is not None and scope.dimensions is not None
        assert scope.domains.allow == ["gl", "payroll"]
        assert scope.domains.deny == ["project_economics", "timesheets"]
        assert scope.dimensions.allow == {
            "cost_centre": ["CC-FINC-01", "CC-GADM-01"]
        }


def test_06_hr_director_domain_only():
    perms = _resolve("06_hr_director.yml")
    for sid in SCENARIOS:
        sp = perms[sid]
        assert sp.effective_role == "manager"
        # All three tool types available; each scoped to domain=[payroll]
        for tt in TOOL_TYPE_MIN_RANK:
            scope = sp.tool_scopes[tt]
            assert scope is not None
            assert scope.domains == DomainScope(allow=["payroll"], deny=None)
            assert scope.dimensions is None


def test_07_overlay_carve_out_deny_only():
    """No allow on domains/dimensions → universe. Deny subtracts specific members."""
    perms = _resolve("07_overlay_carve_out.yml")
    for sid in SCENARIOS:
        sp = perms[sid]
        assert sp.effective_role == "manager"
        scope = sp.tool_scopes["plan_manager"]
        assert scope is not None
        assert scope.domains == DomainScope(allow=None, deny=["payroll"])
        assert scope.dimensions == DimensionScope(
            allow=None, deny={"cost_centre": ["CC-GADM-01", "CC-GADM-02"]}
        )


def test_08_specificity_override_no_merge():
    """Most specific wins — PLAN entry does NOT merge with *, literal does
    NOT merge with PLAN. Verifies the no-merge rule."""
    perms = _resolve("08_specificity_override.yml")

    # BUD-2026: literal wins. Only the manager block (domains=[gl]) applies.
    # The PLAN.planner and *.analyst blocks are ignored entirely.
    sp = perms["BUD-2026"]
    assert sp.effective_role == "manager"
    pm = sp.tool_scopes["plan_manager"]
    assert pm is not None and pm.domains is not None
    assert pm.domains.allow == ["gl"]
    # read tool → only manager block contributes (not *.analyst)
    rd = sp.tool_scopes["read"]
    assert rd is not None and rd.domains is not None
    assert rd.domains.allow == ["gl"]

    # FC-2026-Q1: PLAN wins over *. planner block only; no manager tools.
    sp = perms["FC-2026-Q1"]
    assert sp.effective_role == "planner"
    assert "plan_manager" not in sp.tool_scopes, "PLAN has no manager — must NOT leak from *"
    wr = sp.tool_scopes["write"]
    assert wr is not None and wr.dimensions is not None
    assert wr.dimensions.allow == {"department": ["Data & Analytics"]}

    # ACTUALS: only * matches (no PLAN, no literal).
    sp = perms["ACTUALS"]
    assert sp.effective_role == "manager"
    # Wildcard defines both analyst (narrow) + manager (domain=gl) blocks.
    # plan_manager → manager block. read → analyst block ONLY (not union).
    pm = sp.tool_scopes["plan_manager"]
    assert pm is not None and pm.domains is not None
    assert pm.domains.allow == ["gl"]
    rd = sp.tool_scopes["read"]
    assert rd is not None and rd.dimensions is not None
    # analyst block: cost_centre=[CC-FINC-01]
    assert rd.dimensions.allow == {"cost_centre": ["CC-FINC-01"]}


def test_09_multi_dim_and():
    perms = _resolve("09_multi_dim_and.yml")
    sp = perms["ACTUALS"]
    assert sp.effective_role == "analyst"
    scope = sp.tool_scopes["read"]
    assert scope is not None and scope.dimensions is not None
    # Both dimension keys preserved on allow (AND across keys happens downstream)
    assert scope.dimensions.allow == {
        "department":  ["Software Engineering"],
        "cost_centre": ["CC-SENG-01", "CC-SENG-02", "CC-SENG-03", "CC-SENG-04"],
    }
    assert scope.dimensions.deny == {"cost_centre": ["CC-SENG-04"]}


# ────────────────────────────────────────────────────────────────────────────
# Invalid fixtures — resolver is permissive; validator is a separate layer.
# ────────────────────────────────────────────────────────────────────────────


def test_10_invalid_fixtures_do_not_crash_resolver():
    """The resolver should gracefully skip unknown roles/tokens; catalogue
    validation is the validator's job, not the resolver's."""
    pdef = _load("10_invalid_examples.yml")
    # Unknown role `superuser` in ACTUAL block → filtered by ROLE_RANK check
    blocks = _resolve_profile_blocks(pdef, "ACTUALS", "ACTUAL")
    if blocks:
        for role in blocks:
            assert role in ROLE_RANK, f"resolver leaked unknown role: {role}"
    # Unknown token `SHIFTED` / malformed `PLAN_*` → no match, no crash
    # (Covered implicitly: the above call iterates every scenarios: key.)
