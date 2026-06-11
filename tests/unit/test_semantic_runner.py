# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/semantic_runner.py`.

Pure logic: applies `instance/semantic/{dims,views}/*.sql` as
`CREATE OR REPLACE VIEW semantic.<stem> AS <body>`. Dims are applied
before views so view-time foreign-key resolution can find them.
"""

from __future__ import annotations

from pathlib import Path

from precis_mcp.ingestion.semantic_runner import apply_all


class _StubCH:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)


def _build_semantic_tree(root: Path) -> None:
    dims = root / "dims"
    views = root / "views"
    dims.mkdir(parents=True)
    views.mkdir(parents=True)
    (dims / "dim_account.sql").write_text(
        "SELECT account_code FROM live.dim_account"
    )
    (views / "v_gl.sql").write_text(
        "SELECT period, amount FROM live.fact_gl"
    )


def test_apply_all_creates_semantic_schema(tmp_path: Path):
    _build_semantic_tree(tmp_path)
    ch = _StubCH()
    apply_all(tmp_path, ch)
    assert "CREATE DATABASE IF NOT EXISTS semantic" in ch.commands


def test_apply_all_wraps_each_body_as_create_or_replace_view(tmp_path: Path):
    _build_semantic_tree(tmp_path)
    ch = _StubCH()
    report = apply_all(tmp_path, ch)

    dim_cmd = next(c for c in ch.commands if "semantic.dim_account" in c)
    view_cmd = next(c for c in ch.commands if "semantic.v_gl" in c)
    assert dim_cmd.startswith("CREATE OR REPLACE VIEW semantic.dim_account AS")
    assert view_cmd.startswith("CREATE OR REPLACE VIEW semantic.v_gl AS")
    # Body content preserved.
    assert "FROM live.dim_account" in dim_cmd
    assert "FROM live.fact_gl" in view_cmd
    assert report.views_applied == ["dim_account", "v_gl"]


def test_apply_all_applies_dims_before_views(tmp_path: Path):
    """Views may reference dims via `semantic.dim_<x>`; CH validates the
    references at view-creation time, so dims must already exist."""
    _build_semantic_tree(tmp_path)
    ch = _StubCH()
    apply_all(tmp_path, ch)
    create_cmds = [c for c in ch.commands if "CREATE OR REPLACE VIEW" in c]
    dim_idx = next(
        i for i, c in enumerate(create_cmds) if "semantic.dim_account" in c
    )
    view_idx = next(
        i for i, c in enumerate(create_cmds) if "semantic.v_gl" in c
    )
    assert dim_idx < view_idx


def test_apply_all_sorts_within_each_subdir(tmp_path: Path):
    """Within dims/ and within views/, files are applied in sorted order
    so the runner is deterministic across filesystems."""
    dims = tmp_path / "dims"
    views = tmp_path / "views"
    dims.mkdir(parents=True)
    views.mkdir(parents=True)
    (dims / "z_dim.sql").write_text("SELECT 1")
    (dims / "a_dim.sql").write_text("SELECT 1")
    (views / "z_view.sql").write_text("SELECT 1")
    (views / "a_view.sql").write_text("SELECT 1")

    ch = _StubCH()
    report = apply_all(tmp_path, ch)
    assert report.views_applied == ["a_dim", "z_dim", "a_view", "z_view"]


def test_apply_all_missing_subdir_warns_but_does_not_fail(tmp_path: Path):
    """A semantic dir with only dims/ or only views/ is valid — the
    runner warns about the missing subdir but proceeds."""
    (tmp_path / "dims").mkdir()
    (tmp_path / "dims" / "dim_account.sql").write_text("SELECT 1")
    # No views/ at all.

    ch = _StubCH()
    report = apply_all(tmp_path, ch)
    assert report.views_applied == ["dim_account"]


def test_apply_all_strips_leading_comments_from_body(tmp_path: Path):
    """Leading comment-only / blank lines are stripped so the emitted
    DDL doesn't have `CREATE OR REPLACE VIEW … AS\\n--header comments`
    artefacts cluttering CH logs."""
    dims = tmp_path / "dims"
    dims.mkdir(parents=True)
    (dims / "dim_x.sql").write_text(
        "-- header line\n"
        "\n"
        "SELECT 1"
    )
    ch = _StubCH()
    apply_all(tmp_path, ch)
    cmd = next(c for c in ch.commands if "semantic.dim_x" in c)
    # The line after "AS" is the SELECT, not the comment.
    after_as = cmd.split(" AS\n", 1)[1]
    assert after_as.startswith("SELECT 1")
