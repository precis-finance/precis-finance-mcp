# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""IntegrationRegistry — loader and Pydantic models for Source / Binding.

Reads YAML from `instance/integrations/{sources,bindings}/`, validates
cross-references, and exposes the registry as a typed view used by the
ingestion orchestrator AND the federated read path
(`precis_mcp/engine/ibis_registry.py` — same `Source` objects, single
credential resolution path).

Shape after the Ibis-driven refactor
------------------------------------
The driver protocol is gone. dbt is gone. The landing-table model is
gone. A `Binding` declares:

- `source`        — id of the `Source` to extract from.
- `target`        — fully-qualified live table, e.g. `'live.fact_gl'`.
- `kind`          — 'period' or 'snapshot' (drives swap dispatch).
- `extract.query` — operator-authored SQL run on the source via Ibis.
- `schedule`      — cron / push / watch + period_selection.
- `scenario`      — the scenario this binding writes to (e.g. ACTUALS).

The live and staging table DDLs come from `instance/live/<x>.sql` —
applied by `live_ddl_runner.apply_all`, not declared on the binding.
That's where `PARTITION BY` and `ORDER BY` live now — one declaration,
applied to both schemas.

Backend-specific blocks (`Source.backend`) still accept arbitrary dicts
at this layer; per-source schema validation happens at the Ibis backend
factory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.registry")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntegrationConfigError(Exception):
    """Raised when integration YAML configs fail validation.

    Aborts registry load entirely; the previous registry stays in place
    on reload failure.
    """


# ---------------------------------------------------------------------------
# Shared shape constants
# ---------------------------------------------------------------------------


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_BINDING_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_TARGET_RE = re.compile(r"^live\.[a-z][a-z0-9_]{1,63}$")


def _validate_id(value: str, field: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field}={value!r} must match {_ID_RE.pattern} "
            "(lowercase letters, digits, underscores; start with a letter)"
        )
    return value


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class NetworkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    egress_required: bool
    endpoints: list[str] = Field(default_factory=list)


class Source(BaseModel):
    """One physical origin of data: warehouse, ERP, file drop.

    Serves both the ingestion path (`ibis_executor` resolves an Ibis
    backend per `source.kind`) and the federated read path
    (`engine/ibis_registry.py` uses the same Source by id). One
    declaration, one credential resolution.

    `backend` is a kind-specific dict; per-kind validation happens at
    the Ibis backend factory, not here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    kind: str
    secret_ref: str
    network: NetworkSpec
    backend: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_id(self) -> "Source":
        _validate_id(self.id, "Source.id")
        return self


# ---------------------------------------------------------------------------
# Binding — self-contained, Ibis-driven
# ---------------------------------------------------------------------------


class PeriodSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["lookback", "explicit", "watermark"] = "lookback"
    lookback_periods: int = 3

    @model_validator(mode="after")
    def _check(self) -> "PeriodSelection":
        if self.strategy == "lookback" and self.lookback_periods < 1:
            raise ValueError("lookback_periods must be >= 1")
        return self


class PushAuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    binding_token_ref: str


class WatchSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_glob: str
    period_from: Literal["filename_regex", "column"] = "filename_regex"
    filename_regex: Optional[str] = None
    period_column: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "WatchSpec":
        if self.period_from == "filename_regex" and not self.filename_regex:
            raise ValueError(
                "watch.period_from='filename_regex' requires watch.filename_regex"
            )
        if self.period_from == "column" and not self.period_column:
            raise ValueError(
                "watch.period_from='column' requires watch.period_column"
            )
        return self


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["cron", "push", "watch"]
    expression: Optional[str] = None
    timezone: Optional[str] = None
    period_selection: PeriodSelection = Field(default_factory=PeriodSelection)
    push_auth: Optional[PushAuthSpec] = None
    watch: Optional[WatchSpec] = None

    @model_validator(mode="after")
    def _check(self) -> "Schedule":
        if self.mode == "cron":
            if not self.expression or not self.timezone:
                raise ValueError(
                    "schedule.mode='cron' requires both expression and timezone"
                )
        elif self.mode == "push":
            if not self.push_auth:
                raise ValueError("schedule.mode='push' requires push_auth")
        elif self.mode == "watch":
            if not self.watch:
                raise ValueError("schedule.mode='watch' requires watch config")
        return self


class ManifestConfig(BaseModel):
    """File-drop manifest tracking — used by the watch-mode loader to
    decide when a set of files for a period is complete. Optional;
    not used by warehouse-source bindings."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    source_manifest_table: Optional[str] = None
    source_manifest_filename_pattern: Optional[str] = None


class ExtractSpec(BaseModel):
    """The operator-authored extract for one binding.

    `query` is dialect-native SQL for the binding's source. The
    `:period` placeholder is substituted at execute time with the
    scheduled period literal (orchestrator-validated, regex-bounded
    value space — no injection surface). For `kind='snapshot'`
    bindings, the query has no `:period` reference.

    `ExtractSpec` is intentionally tight — single field today, but
    typed so future additions (timeouts, max_rows, batch hints) get a
    declared schema rather than another untyped dict.
    """

    model_config = ConfigDict(extra="forbid")

    query: str

    @model_validator(mode="after")
    def _check(self) -> "ExtractSpec":
        if not self.query.strip():
            raise ValueError("extract.query must be non-empty")
        return self


class Binding(BaseModel):
    """How one source delivers one live target — schedule, extract
    query, scenario, kind.

    Self-contained: the live and staging table DDLs (engine,
    PARTITION BY, ORDER BY, columns) come from `instance/live/<x>.sql`
    applied by `live_ddl_runner.apply_all`. Nothing about the table's
    physical shape lives on the binding.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str  # 'live.<table>'
    scenario: str
    kind: Literal["period", "snapshot"]
    schedule: Schedule
    extract: ExtractSpec
    manifest: Optional[ManifestConfig] = None

    @model_validator(mode="after")
    def _check(self) -> "Binding":
        if not _BINDING_ID_RE.match(self.id):
            raise ValueError(
                f"Binding.id={self.id!r} must match {_BINDING_ID_RE.pattern}"
            )
        if not _TARGET_RE.match(self.target):
            raise ValueError(
                f"Binding {self.id!r}: target={self.target!r} must match "
                f"{_TARGET_RE.pattern} (i.e. 'live.<lowercase_table_name>')"
            )
        return self

    # -- Derived names --------------------------------------------------------

    @property
    def table_name(self) -> str:
        """Bare table name (no schema). `live.fact_gl` → `fact_gl`."""
        return self.target[len("live."):]

    @property
    def staging_target(self) -> str:
        """Staging twin. `live.fact_gl` → `staging.fact_gl`. Same name
        in a different schema — `live_ddl_runner.apply_all` enforces
        this invariant."""
        return f"staging.{self.table_name}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class IntegrationRegistry:
    """In-memory view of the active integration configuration.

    Loaded from a directory tree of YAML files
    (`instance/integrations/{sources,bindings}/`). Validation is
    atomic — a failed reload leaves the previous registry in place.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.sources: dict[str, Source] = {}
        self.bindings: dict[str, Binding] = {}

    # -- Public API ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        root: Path | str,
        *,
        secret_check_env: Optional[dict[str, str]] = None,
    ) -> "IntegrationRegistry":
        """Load and validate the registry from a directory.

        `root` points at a directory containing `sources/` and
        `bindings/` subdirectories (typically `instance/integrations/`
        in production, an isolated path in tests).

        Raises IntegrationConfigError on any validation failure. After
        structural validation, emits WARN-level events for two soft
        conditions that don't block load:

        - A `secret_ref` with no matching env vars in the resolved
          environment.
        - A Source declared with no Bindings.

        `secret_check_env` is injectable for tests; production passes
        None and the implementation reads `os.environ`.
        """
        registry = cls(Path(root))
        registry._load_all()
        registry._validate_cross_references()
        registry._warn_soft_conditions(secret_check_env)
        _logger.info(
            "ingestion.registry.loaded",
            root=str(registry.root),
            sources=len(registry.sources),
            bindings=len(registry.bindings),
        )
        return registry

    def get_binding(self, binding_id: str) -> Binding:
        try:
            return self.bindings[binding_id]
        except KeyError:
            raise IntegrationConfigError(f"Unknown binding: {binding_id!r}")

    def get_source(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError:
            raise IntegrationConfigError(f"Unknown source: {source_id!r}")

    def bindings_for_source(self, source_id: str) -> list[Binding]:
        return [b for b in self.bindings.values() if b.source == source_id]

    def bindings_for_target(self, target: str) -> list[Binding]:
        """All bindings writing to a given live table. Typically one;
        cross-validation enforces target uniqueness."""
        return [b for b in self.bindings.values() if b.target == target]

    # -- Loading ------------------------------------------------------------

    def _load_all(self) -> None:
        self._load_dir("sources", Source, self.sources)
        self._load_dir("bindings", Binding, self.bindings)

    def _load_dir(
        self,
        subdir: str,
        model: type[BaseModel],
        target: dict[str, Any],
    ) -> None:
        dir_path = self.root / subdir
        if not dir_path.exists():
            # An empty registry is valid; an evaluation deployment may
            # start with no integrations declared.
            return
        for yml in sorted(dir_path.glob("*.yml")):
            try:
                raw = yaml.safe_load(yml.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise IntegrationConfigError(
                    f"YAML parse failure in {yml}: {exc}"
                ) from exc
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise IntegrationConfigError(
                    f"{yml}: top-level YAML must be a mapping, got "
                    f"{type(raw).__name__}"
                )
            try:
                obj = model.model_validate(raw)
            except Exception as exc:
                raise IntegrationConfigError(
                    f"{yml}: validation failed — {exc}"
                ) from exc
            obj_id = getattr(obj, "id")
            if obj_id in target:
                raise IntegrationConfigError(
                    f"{yml}: duplicate {model.__name__} id {obj_id!r} "
                    f"(also in another file)"
                )
            target[obj_id] = obj

    # -- Cross-reference validation ----------------------------------------

    def _validate_cross_references(self) -> None:
        # Every binding references a known source.
        for binding in self.bindings.values():
            if binding.source not in self.sources:
                raise IntegrationConfigError(
                    f"Binding {binding.id!r}: source {binding.source!r} "
                    f"not found in sources/ — known: {sorted(self.sources)}"
                )

        # Target uniqueness: at most one binding writes to each
        # `live.<table>`. Two bindings on the same target would race for
        # the swap.
        seen: dict[str, str] = {}
        for binding in self.bindings.values():
            if binding.target in seen:
                raise IntegrationConfigError(
                    f"Two bindings target the same {binding.target!r}: "
                    f"{seen[binding.target]!r} and {binding.id!r}"
                )
            seen[binding.target] = binding.id

    # -- Soft warnings ------------------------------------------------------

    def _warn_soft_conditions(
        self,
        secret_check_env: Optional[dict[str, str]],
    ) -> None:
        """Emit WARN for orphan sources and missing-secret refs.
        Non-blocking."""
        import os

        env = secret_check_env if secret_check_env is not None else dict(os.environ)

        for source in self.sources.values():
            if not self.bindings_for_source(source.id):
                _logger.warning(
                    "ingestion.registry.orphan_source",
                    source_id=source.id,
                    kind=source.kind,
                    hint=(
                        "declare a binding under integrations/bindings/ or "
                        "remove the source"
                    ),
                )

            prefix = source.secret_ref.upper() + "_"
            has_any = (
                any(k.startswith(prefix) for k in env)
                or source.secret_ref.upper() in env
            )
            if not has_any:
                _logger.warning(
                    "ingestion.registry.secret_ref_missing",
                    source_id=source.id,
                    secret_ref=source.secret_ref,
                    expected_prefix=prefix,
                    hint=(
                        "set env vars matching the driver's secret schema, "
                        "or check secret_ref typo"
                    ),
                )