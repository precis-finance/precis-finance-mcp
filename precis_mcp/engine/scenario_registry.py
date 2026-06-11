# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unified scenario registry for reporting and planning boundaries.

This module is intentionally independent from ``Catalogue.scenarios``. It
models real data-holding scenarios from ``semantic.scenarios`` and derives the
flat reporting keys that replace YAML-maintained shifted/computed scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


COMPATIBILITY_ALIASES = {
    "prior_year": "actuals_py",
    "prior_period": "actuals_pp",
}

RESERVED_ALIAS_FRAGMENTS = ("_vs_",)
RESERVED_ALIAS_SUFFIXES = ("_py", "_pp", "_pct")


class ScenarioRegistryError(Exception):
    """Base error for scenario registry failures."""


class UnknownScenarioError(ScenarioRegistryError):
    """Raised when a scenario key/id/alias cannot be resolved."""


class InvalidScenarioAliasError(ScenarioRegistryError):
    """Raised when a real scenario alias conflicts with generated key syntax."""


class NonWritableScenarioError(ScenarioRegistryError):
    """Raised when a write/admin path targets a generated scenario."""


@dataclass(frozen=True)
class RealScenarioRef:
    key: str
    scenario_id: str
    alias: str
    name: str
    description: str = ""
    kind: str = ""
    status: str = ""
    base_scenario: str | None = None
    variant_of: str | None = None
    horizon_start: str = ""
    horizon_end: str = ""
    actuals_cutoff: str | None = None
    granularity: str = "monthly"
    owner_user_id: str = ""
    locks: str = "[]"
    created_by: str = ""
    created_at: Any = None
    updated_at: Any = None
    node_type: Literal["real"] = "real"


@dataclass(frozen=True)
class ShiftedScenarioRef:
    key: str
    base: RealScenarioRef
    time_offset_months: int
    label: str
    description: str
    node_type: Literal["shifted"] = "shifted"


@dataclass(frozen=True)
class VarianceScenarioRef:
    key: str
    left: RealScenarioRef | ShiftedScenarioRef
    right: RealScenarioRef | ShiftedScenarioRef
    label: str
    description: str
    node_type: Literal["variance"] = "variance"


@dataclass(frozen=True)
class VariancePctScenarioRef:
    key: str
    left: RealScenarioRef | ShiftedScenarioRef
    right: RealScenarioRef | ShiftedScenarioRef
    label: str
    description: str
    node_type: Literal["variance_pct"] = "variance_pct"


ScenarioRef = (
    RealScenarioRef
    | ShiftedScenarioRef
    | VarianceScenarioRef
    | VariancePctScenarioRef
)


class ScenarioRegistry:
    """Resolve scenario aliases, IDs, and generated reporting keys."""

    def __init__(self, real_scenarios: list[RealScenarioRef]):
        aliases: dict[str, RealScenarioRef] = {}
        ids: dict[str, RealScenarioRef] = {}

        for scenario in real_scenarios:
            _validate_alias(scenario.alias)
            if scenario.alias in aliases:
                raise InvalidScenarioAliasError(
                    f"Duplicate scenario alias: {scenario.alias!r}"
                )
            if scenario.scenario_id in ids:
                raise InvalidScenarioAliasError(
                    f"Duplicate scenario_id: {scenario.scenario_id!r}"
                )
            aliases[scenario.alias] = scenario
            ids[scenario.scenario_id] = scenario

        self._aliases = aliases
        self._ids = ids

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "ScenarioRegistry":
        """Build a registry from ``semantic.scenarios``-shaped rows."""
        return cls([_real_from_row(row) for row in rows])

    @property
    def real_scenarios(self) -> list[RealScenarioRef]:
        return sorted(self._aliases.values(), key=lambda s: s.alias)

    def normalize_key(self, value: str) -> str:
        """Return the public registry key for a user-supplied ref."""
        ref = self.resolve_key(value)
        return ref.key

    def resolve_key(self, value: str) -> ScenarioRef:
        """Resolve a real alias/id or generated key to a scenario ref."""
        if not value:
            raise UnknownScenarioError("Scenario reference is empty.")

        key = COMPATIBILITY_ALIASES.get(value, value)

        real = self._aliases.get(key) or self._ids.get(key)
        if real is not None:
            return real

        shifted = self._resolve_shifted(key)
        if shifted is not None:
            return shifted

        variance = self._resolve_variance(key)
        if variance is not None:
            return variance

        raise UnknownScenarioError(f"Unknown scenario: {value!r}")

    def resolve_read_ref(self, value: str) -> ScenarioRef:
        """Resolve a scenario reference for read/reporting execution."""
        return self.resolve_key(value)

    def resolve_writable(self, value: str) -> RealScenarioRef:
        """Resolve a write/admin target, rejecting generated scenarios."""
        ref = self.resolve_key(value)
        if isinstance(ref, RealScenarioRef):
            return ref
        raise NonWritableScenarioError(
            f"Cannot write to scenario {value!r}: generated {ref.node_type} "
            "scenarios are read-only."
        )

    def expand_dependencies(self, value: str) -> set[str]:
        """Return underlying real ``scenario_id`` values for any ref."""
        ref = self.resolve_key(value)
        return _dependencies(ref)

    def get_kind_map(self) -> dict[str, str]:
        return {
            scenario.scenario_id: scenario.kind
            for scenario in self._aliases.values()
            if scenario.kind
        }

    def list_real_scenarios(self) -> list[RealScenarioRef]:
        return self.real_scenarios

    def get_real_scenario(self, value: str) -> RealScenarioRef:
        return self.resolve_writable(value)

    def list_writable_scenarios(self) -> list[RealScenarioRef]:
        return self.real_scenarios

    def list_variants(self, parent: str) -> list[RealScenarioRef]:
        parent_ref = self.resolve_writable(parent)
        return sorted(
            [
                scenario
                for scenario in self._aliases.values()
                if scenario.variant_of == parent_ref.scenario_id
            ],
            key=lambda s: s.alias,
        )

    def label_for(self, value: str) -> str:
        ref = self.resolve_key(value)
        if isinstance(ref, RealScenarioRef):
            return ref.name
        return ref.label

    def description_for(self, value: str) -> str:
        ref = self.resolve_key(value)
        return ref.description

    def display_format_for(self, value: str, metric_format: str = "") -> str:
        ref = self.resolve_key(value)
        if isinstance(ref, VariancePctScenarioRef):
            return "percent"
        return metric_format

    def color_code_for(self, value: str) -> bool:
        ref = self.resolve_key(value)
        return isinstance(ref, VarianceScenarioRef | VariancePctScenarioRef)

    def get_governance_metadata(self, value: str) -> dict:
        return _real_metadata_row(self.get_real_scenario(value))

    def list_reporting_scenarios(self, policy: Any | None = None) -> list[dict]:
        """Return generated reporting vocabulary entries.

        ``policy`` is reserved for the future prompt/discovery policy layer.
        Current behavior intentionally returns the broad deterministic
        vocabulary for the loaded real scenario set.
        """
        del policy
        entries: list[dict] = []
        entries.extend(self.to_reporting_vocabulary()["real"])
        entries.extend(self.to_reporting_vocabulary()["shifted"])
        entries.extend(self.to_reporting_vocabulary()["comparisons"])
        return entries

    def list_prompt_scenarios(self, policy: Any | None = None) -> list[dict]:
        return self.list_reporting_scenarios(policy)

    def to_real_metadata_rows(self) -> list[dict]:
        return [_real_metadata_row(s) for s in self.list_real_scenarios()]

    def to_validation_scenario_rows(self) -> list[dict]:
        return [
            {
                "scenario_id": s.scenario_id,
                "alias": s.alias,
                "name": s.name,
                "status": s.status,
                "description": s.description,
                "kind": s.kind,
            }
            for s in self.list_real_scenarios()
        ]

    def to_reporting_vocabulary(self) -> dict:
        shifted: list[dict] = []
        comparisons: list[dict] = []
        real_refs = self.list_real_scenarios()

        for real in real_refs:
            for suffix in ("_py", "_pp"):
                shifted.append(_shifted_metadata_row(
                    self.resolve_key(f"{real.alias}{suffix}")
                ))

        for left in real_refs:
            for right in real_refs:
                if left.alias == right.alias:
                    continue
                comparisons.append(_comparison_metadata_row(
                    self.resolve_key(f"{left.alias}_vs_{right.alias}"),
                    self,
                ))
                comparisons.append(_comparison_metadata_row(
                    self.resolve_key(f"{left.alias}_vs_{right.alias}_pct"),
                    self,
                ))

        # Self-YoY comparisons: <alias> vs <alias>_py, signed and percentage.
        # The registry resolves arbitrary shifted-vs-anything keys, but the
        # curated vocabulary only enumerates the universal FP&A case (a scenario
        # compared to its own prior year) to keep the prompt and list_scenarios
        # output bounded at 2N additional entries.
        for real in real_refs:
            comparisons.append(_comparison_metadata_row(
                self.resolve_key(f"{real.alias}_vs_{real.alias}_py"),
                self,
            ))
            comparisons.append(_comparison_metadata_row(
                self.resolve_key(f"{real.alias}_vs_{real.alias}_py_pct"),
                self,
            ))

        return {
            "real": self.to_real_metadata_rows(),
            "shifted": shifted,
            "comparisons": comparisons,
            "compatibility_aliases": [
                {"key": key, "resolves_to": value}
                for key, value in COMPATIBILITY_ALIASES.items()
            ],
        }

    def _resolve_shifted(self, key: str) -> ShiftedScenarioRef | None:
        for suffix, offset, label_suffix in (
            ("_py", -12, "PY"),
            ("_pp", -1, "PP"),
        ):
            if not key.endswith(suffix):
                continue
            base_key = key[: -len(suffix)]
            base = self._aliases.get(base_key)
            if base is None:
                return None
            return ShiftedScenarioRef(
                key=f"{base.alias}{suffix}",
                base=base,
                time_offset_months=offset,
                label=f"{base.name} {label_suffix}",
                description=(
                    f"{base.name} shifted by {offset} month"
                    f"{'' if abs(offset) == 1 else 's'}."
                ),
            )
        return None

    def _resolve_variance(self, key: str) -> ScenarioRef | None:
        is_pct = key.endswith("_pct")
        body = key[:-4] if is_pct else key
        if "_vs_" not in body:
            return None

        left_key, right_key = body.split("_vs_", 1)
        try:
            left = self.resolve_key(left_key)
            right = self.resolve_key(right_key)
        except UnknownScenarioError:
            return None

        if not isinstance(left, RealScenarioRef | ShiftedScenarioRef):
            return None
        if not isinstance(right, RealScenarioRef | ShiftedScenarioRef):
            return None

        label = f"{_label(left)} vs {_label(right)}"
        description = f"{label}: {_label(left)} minus {_label(right)}."
        if is_pct:
            return VariancePctScenarioRef(
                key=f"{left.key}_vs_{right.key}_pct",
                left=left,
                right=right,
                label=f"{label} %",
                description=f"{description} Percentage variance vs comparator.",
            )
        return VarianceScenarioRef(
            key=f"{left.key}_vs_{right.key}",
            left=left,
            right=right,
            label=label,
            description=description,
        )


def _validate_alias(alias: str) -> None:
    if not alias:
        raise InvalidScenarioAliasError("Scenario alias is required.")
    for fragment in RESERVED_ALIAS_FRAGMENTS:
        if fragment in alias:
            raise InvalidScenarioAliasError(
                f"Scenario alias {alias!r} conflicts with generated key syntax."
            )
    for suffix in RESERVED_ALIAS_SUFFIXES:
        if alias.endswith(suffix):
            raise InvalidScenarioAliasError(
                f"Scenario alias {alias!r} conflicts with generated suffix {suffix!r}."
            )
    if alias in COMPATIBILITY_ALIASES:
        raise InvalidScenarioAliasError(
            f"Scenario alias {alias!r} is reserved for compatibility behavior."
        )


def _real_from_row(row: dict[str, Any]) -> RealScenarioRef:
    scenario_id = str(row["scenario_id"])
    alias = str(row.get("alias") or "")
    return RealScenarioRef(
        key=alias,
        scenario_id=scenario_id,
        alias=alias,
        name=str(row.get("name") or alias or scenario_id),
        description=str(row.get("description") or ""),
        kind=str(row.get("kind") or ""),
        status=str(row.get("status") or ""),
        base_scenario=row.get("base_scenario"),
        variant_of=row.get("variant_of"),
        horizon_start=str(row.get("horizon_start") or ""),
        horizon_end=str(row.get("horizon_end") or ""),
        actuals_cutoff=row.get("actuals_cutoff"),
        granularity=str(row.get("granularity") or "monthly"),
        owner_user_id=str(row.get("owner_user_id") or ""),
        locks=str(row.get("locks") or "[]"),
        created_by=str(row.get("created_by") or ""),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _dependencies(ref: ScenarioRef) -> set[str]:
    if isinstance(ref, RealScenarioRef):
        return {ref.scenario_id}
    if isinstance(ref, ShiftedScenarioRef):
        return {ref.base.scenario_id}
    return _dependencies(ref.left) | _dependencies(ref.right)


def _label(ref: RealScenarioRef | ShiftedScenarioRef) -> str:
    if isinstance(ref, RealScenarioRef):
        return ref.name
    return ref.label


def _real_metadata_row(s: RealScenarioRef) -> dict:
    return {
        "key": s.key,
        "alias": s.alias,
        "scenario_id": s.scenario_id,
        "label": s.name,
        "name": s.name,
        "description": s.description,
        "type": "real",
        "status": s.status,
        "kind": s.kind,
        "base_scenario": s.base_scenario,
        "variant_of": s.variant_of,
        "horizon_start": s.horizon_start,
        "horizon_end": s.horizon_end,
        "actuals_cutoff": s.actuals_cutoff,
        "granularity": s.granularity,
        "owner_user_id": s.owner_user_id,
        "updated_at": str(s.updated_at) if s.updated_at is not None else None,
    }


def _shifted_metadata_row(s: ScenarioRef) -> dict:
    if not isinstance(s, ShiftedScenarioRef):
        raise TypeError("Expected shifted scenario ref")
    return {
        "key": s.key,
        "label": s.label,
        "description": s.description,
        "type": "shifted",
        "base": s.base.alias,
        "base_scenario_id": s.base.scenario_id,
        "time_offset_months": s.time_offset_months,
    }


def _comparison_metadata_row(s: ScenarioRef, registry: ScenarioRegistry) -> dict:
    if not isinstance(s, VarianceScenarioRef | VariancePctScenarioRef):
        raise TypeError("Expected comparison scenario ref")
    return {
        "key": s.key,
        "label": s.label,
        "description": s.description,
        "type": s.node_type,
        "left": s.left.key,
        "right": s.right.key,
        "scenario_ids": sorted(registry.expand_dependencies(s.key)),
        "color_code": True,
        "display_format": "percent" if isinstance(s, VariancePctScenarioRef) else "",
    }


def load_scenario_registry(client: Any) -> ScenarioRegistry:
    """Load ``ScenarioRegistry`` from a ClickHouse client."""
    result = client.query("""
        SELECT scenario_id, alias, name, base_scenario, status, description,
               created_by, created_at, locked_at, horizon_start, horizon_end,
               actuals_cutoff, granularity, owner_user_id, updated_at,
               variant_of, locks, kind
        FROM semantic.scenarios
        ORDER BY scenario_id
    """)
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return ScenarioRegistry.from_rows(rows)
