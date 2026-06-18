# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/registry.py`.

Pure logic: YAML loading from disk under `tmp_path`, Pydantic
validation, cross-reference checks (source FK, target uniqueness).
No external services.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis_mcp.ingestion.registry import (
    Binding,
    IntegrationConfigError,
    IntegrationRegistry,
    Source,
)
from tests.factories.ingestion import build_tree, make_binding, make_source, write_yaml


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------


def test_source_loads_with_minimal_postgres_shape():
    src = Source.model_validate(make_source("test_pg", kind="postgres"))
    assert src.id == "test_pg"
    assert src.kind == "postgres"


def test_source_id_must_match_id_regex():
    raw = make_source("BadID")  # uppercase rejected
    with pytest.raises(ValueError):
        Source.model_validate(raw)


# ---------------------------------------------------------------------------
# Binding validation
# ---------------------------------------------------------------------------


def test_binding_loads_with_minimal_period_shape():
    bd = Binding.model_validate(make_binding())
    assert bd.target == "live.fact_gl"
    assert bd.kind == "period"
    assert bd.extract.query.strip().startswith("SELECT")


def test_binding_target_must_start_with_live_prefix():
    raw = make_binding(target="warehouse.fact_gl")
    with pytest.raises(ValueError, match="must match"):
        Binding.model_validate(raw)


def test_binding_target_must_match_table_name_regex():
    raw = make_binding(target="live.BadName")  # uppercase rejected
    with pytest.raises(ValueError, match="must match"):
        Binding.model_validate(raw)


def test_binding_extract_query_must_be_non_empty():
    raw = make_binding(extract_query="   ")
    with pytest.raises(ValueError, match="non-empty"):
        Binding.model_validate(raw)


def test_binding_rejects_unknown_field():
    """Pydantic `extra='forbid'` catches stale YAMLs that carry deleted
    fields (`dataset`, `partition_expression`, etc.)."""
    raw = make_binding()
    raw["dataset"] = "gl"  # deleted field
    with pytest.raises(ValueError):
        Binding.model_validate(raw)


# ---------------------------------------------------------------------------
# Derived properties
# ---------------------------------------------------------------------------


def test_binding_table_name_strips_live_prefix():
    bd = Binding.model_validate(make_binding(target="live.fact_gl"))
    assert bd.table_name == "fact_gl"


def test_binding_staging_target_swaps_schema():
    bd = Binding.model_validate(make_binding(target="live.dim_client"))
    assert bd.staging_target == "staging.dim_client"


# ---------------------------------------------------------------------------
# Registry load + cross-validation
# ---------------------------------------------------------------------------


def test_registry_loads_minimal_tree(tmp_path: Path):
    root = build_tree(tmp_path)
    reg = IntegrationRegistry.load(root, secret_check_env={})
    assert set(reg.sources) == {"test_pg"}
    assert set(reg.bindings) == {"test_pg__fact_gl"}


def test_registry_load_rejects_unknown_source_reference(tmp_path: Path):
    src = make_source("real_source")
    bd = make_binding(source="ghost_source")
    root = build_tree(tmp_path, sources=[src], bindings=[bd])
    with pytest.raises(IntegrationConfigError, match="not found in sources"):
        IntegrationRegistry.load(root, secret_check_env={})


def test_registry_load_rejects_duplicate_target(tmp_path: Path):
    """Two bindings writing to the same `live.<x>` would race on swap —
    the registry catches it at load."""
    s1 = make_source("src_a")
    s2 = make_source("src_b")
    b1 = make_binding(binding_id="a__fact_gl", source="src_a", target="live.fact_gl")
    b2 = make_binding(binding_id="b__fact_gl", source="src_b", target="live.fact_gl")
    root = build_tree(tmp_path, sources=[s1, s2], bindings=[b1, b2])
    with pytest.raises(IntegrationConfigError, match="target the same"):
        IntegrationRegistry.load(root, secret_check_env={})


def test_registry_load_rejects_duplicate_binding_id(tmp_path: Path):
    src = make_source("src_a")
    b1 = make_binding(binding_id="dup", source="src_a", target="live.fact_one")
    root = build_tree(tmp_path, sources=[src], bindings=[b1])
    # Write a second binding YAML with the same id but a different filename.
    write_yaml(
        root / "bindings" / "dup_again.yml",
        make_binding(binding_id="dup", source="src_a", target="live.fact_two"),
    )
    with pytest.raises(IntegrationConfigError, match="duplicate"):
        IntegrationRegistry.load(root, secret_check_env={})


def test_registry_lookup_methods(tmp_path: Path):
    s1 = make_source("src_a")
    s2 = make_source("src_b")
    b1 = make_binding(binding_id="a__fact_gl", source="src_a", target="live.fact_gl")
    b2 = make_binding(binding_id="b__fact_p", source="src_b", target="live.fact_pipeline")
    root = build_tree(tmp_path, sources=[s1, s2], bindings=[b1, b2])
    reg = IntegrationRegistry.load(root, secret_check_env={})

    assert reg.get_binding("a__fact_gl").source == "src_a"
    assert reg.get_source("src_a").id == "src_a"
    assert [b.id for b in reg.bindings_for_source("src_a")] == ["a__fact_gl"]
    assert [b.id for b in reg.bindings_for_target("live.fact_pipeline")] == ["b__fact_p"]


def test_registry_get_binding_raises_on_unknown(tmp_path: Path):
    root = build_tree(tmp_path)
    reg = IntegrationRegistry.load(root, secret_check_env={})
    with pytest.raises(IntegrationConfigError, match="Unknown binding"):
        reg.get_binding("ghost")


def test_registry_empty_dir_yields_empty_registry(tmp_path: Path):
    """Operator-style deployment may bootstrap with no YAML; the
    registry should load empty rather than raise."""
    root = tmp_path / "empty"
    root.mkdir()
    reg = IntegrationRegistry.load(root, secret_check_env={})
    assert reg.sources == {}
    assert reg.bindings == {}
