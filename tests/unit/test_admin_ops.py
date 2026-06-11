# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the open admin-ops core (precis_mcp/admin_ops.py).

Pure logic only — schemas, profile parsing/rendering, password validation,
and the domain-exception → status/exit mappings. No DB, no network.
"""
from __future__ import annotations

import pytest

from precis_mcp import admin_ops as ops


# --- password validation ---------------------------------------------------


def test_validate_password_too_short_raises():
    with pytest.raises(ops.AdminValidationError):
        ops.validate_password("short")


def test_validate_password_ok():
    ops.validate_password("a-perfectly-fine-password")  # no raise


# --- Profile schema --------------------------------------------------------


_VALID_PROFILE = {
    "profile_id": "budget-analyst",
    "name": "Budget Analyst",
    "description": "Reads budgets",
    "scenarios": {"BUDGET": {"analyst": {}}},
}


def test_profile_accepts_valid_tree():
    p = ops.Profile.model_validate(_VALID_PROFILE)
    assert p.profile_id == "budget-analyst"
    assert "BUDGET" in p.scenarios


def test_profile_rejects_bad_id_pattern():
    bad = {**_VALID_PROFILE, "profile_id": "Has Spaces"}
    with pytest.raises(Exception):  # pydantic ValidationError
        ops.Profile.model_validate(bad)


def test_profile_forbids_extra_fields():
    bad = {**_VALID_PROFILE, "unexpected": 1}
    with pytest.raises(Exception):
        ops.Profile.model_validate(bad)


# --- parse_profile_body ----------------------------------------------------


def test_parse_profile_body_yaml_path():
    raw = (
        "profile_id: p1\nname: P One\ndescription: d\n"
        "scenarios:\n  '*':\n    analyst: {}\n"
    )
    pid, name, desc, definition = ops.parse_profile_body(
        ops.ProfileYamlBody(yaml=raw)
    )
    assert pid == "p1"
    assert name == "P One"
    assert definition == {"scenarios": {"*": {"analyst": {}}}}


def test_parse_profile_body_data_path():
    pid, name, _desc, definition = ops.parse_profile_body(
        ops.ProfileYamlBody(data=_VALID_PROFILE)
    )
    assert pid == "budget-analyst"
    assert definition["scenarios"]["BUDGET"] == {"analyst": {}}


def test_parse_profile_body_both_raises():
    with pytest.raises(ops.AdminValidationError):
        ops.parse_profile_body(ops.ProfileYamlBody(yaml="x: 1", data={"y": 2}))


def test_parse_profile_body_neither_raises():
    with pytest.raises(ops.AdminValidationError):
        ops.parse_profile_body(ops.ProfileYamlBody())


def test_parse_profile_yaml_invalid_yaml_raises():
    with pytest.raises(ops.AdminValidationError):
        ops.parse_profile_yaml("key: : :")


def test_parse_profile_yaml_non_mapping_raises():
    with pytest.raises(ops.AdminValidationError):
        ops.parse_profile_yaml("- just\n- a\n- list\n")


# --- profile_yaml_repr (round-trip) ----------------------------------------


def test_profile_yaml_repr_round_trips():
    row = {
        "profile_id": "p1",
        "name": "P One",
        "description": "d",
        "definition": {"scenarios": {"*": {"analyst": {}}}},
    }
    rendered = ops.profile_yaml_repr(row)
    pid, name, _desc, definition = ops.parse_profile_body(
        ops.ProfileYamlBody(yaml=rendered)
    )
    assert pid == "p1"
    assert name == "P One"
    assert definition == {"scenarios": {"*": {"analyst": {}}}}


def test_profile_yaml_repr_accepts_json_string_definition():
    row = {
        "profile_id": "p1",
        "name": "P One",
        "definition": '{"scenarios": {"*": {"manager": {}}}}',
    }
    rendered = ops.profile_yaml_repr(row)
    assert "manager" in rendered


# --- schema registry + JSON-schema null stripping --------------------------


def test_admin_schema_registry_has_profile():
    reg = ops.admin_schema_registry()
    assert "profile" in reg and reg["profile"] is ops.Profile


def test_strip_null_from_anyof_collapses_optional_union():
    node = {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "X"}
    out = ops.strip_null_from_anyof(node)
    assert out == {"type": "string", "title": "X"}


# --- exception → status / exit mappings ------------------------------------


def test_status_and_exit_mappings_cover_all_subtypes():
    for exc in (
        ops.NotFoundError,
        ops.ConflictError,
        ops.AdminValidationError,
        ops.ProvisioningError,
    ):
        assert exc in ops.ADMIN_HTTP_STATUS
        assert exc in ops.ADMIN_EXIT_CODE
        assert issubclass(exc, ops.AdminError)
