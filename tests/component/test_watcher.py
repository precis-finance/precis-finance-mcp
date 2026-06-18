# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the object-store watcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis_mcp.ingestion.object_store import InMemoryObjectStore
from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.watcher import Watcher

from tests.factories.ingestion import (
    build_tree,
    make_binding,
    make_orchestrator_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx_with_watch_binding(tmp_path: Path):
    """Build a registry with one binding using schedule.mode='watch'."""
    # Default make_binding already has watch mode.
    root = build_tree(tmp_path, bindings=[make_binding(schedule_mode="watch")])
    registry = IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )

    ctx, history, _ch, _ibis = make_orchestrator_context(registry)
    # Extend the fake to support processed_watch_keys_for_binding.
    history.processed_watch_keys_for_binding = lambda binding_id: {
        row["triggered_by"][len("watch:"):]
        for row in history.rows.values()
        if row["binding_id"] == binding_id
        and row.get("triggered_by", "").startswith("watch:")
    }
    return ctx, history


# ---------------------------------------------------------------------------
# tick — happy path
# ---------------------------------------------------------------------------


def test_tick_fires_run_binding_for_new_file(tmp_path: Path):
    ctx, history = _make_ctx_with_watch_binding(tmp_path)
    store = InMemoryObjectStore()
    store.put("incoming/gl_2026-04.csv", b"any content")

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    result = watcher.tick()

    assert result.bindings_inspected == 1
    assert result.files_new == 1
    assert result.loads_fired == 1
    # A load_history row was created.
    rows = [r for r in history.rows.values() if r["status"] == "success"]
    assert len(rows) == 1
    assert rows[0]["triggered_by"] == "watch:incoming/gl_2026-04.csv"
    assert rows[0]["period"] == "2026-04"


def test_tick_skips_already_processed_file(tmp_path: Path):
    ctx, history = _make_ctx_with_watch_binding(tmp_path)
    store = InMemoryObjectStore()
    store.put("incoming/gl_2026-04.csv", b"x")

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    # First tick processes; second is a no-op.
    first = watcher.tick()
    second = watcher.tick()
    assert first.loads_fired == 1
    assert second.loads_fired == 0
    assert second.files_seen == 1  # still listed, just not re-processed


def test_tick_handles_multiple_new_files(tmp_path: Path):
    ctx, _ = _make_ctx_with_watch_binding(tmp_path)
    store = InMemoryObjectStore()
    store.put("incoming/gl_2026-04.csv", b"x")
    store.put("incoming/gl_2026-05.csv", b"y")
    store.put("incoming/gl_2026-06.csv", b"z")

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    result = watcher.tick()
    assert result.loads_fired == 3


def test_tick_isolates_bindings_with_separate_processed_keys(tmp_path: Path):
    """Two bindings each have their own processed-keys set."""
    ctx, history = _make_ctx_with_watch_binding(tmp_path)
    store = InMemoryObjectStore()
    store.put("incoming/gl_2026-04.csv", b"x")

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    watcher.tick()
    assert any(
        r["binding_id"] == "test_pg__fact_gl"
        for r in history.rows.values()
    )


# ---------------------------------------------------------------------------
# Period inference failure handling
# ---------------------------------------------------------------------------


def test_tick_skips_file_when_period_cannot_be_inferred(tmp_path: Path):
    ctx, _ = _make_ctx_with_watch_binding(tmp_path)
    store = InMemoryObjectStore()
    # File matches the glob but the period substring is malformed.
    store.put("incoming/gl_NOTAPERIOD.csv", b"x")

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    result = watcher.tick()
    # File was seen but period inference failed; load did not fire.
    assert result.files_seen == 1
    assert result.loads_skipped_period_inference == 1
    assert result.loads_fired == 0


# ---------------------------------------------------------------------------
# Bindings with non-watch schedules are skipped
# ---------------------------------------------------------------------------


def test_tick_skips_non_watch_bindings(tmp_path: Path):
    from tests.factories.ingestion import build_tree, make_binding, make_source

    bd = make_binding()
    bd["schedule"] = {
        "mode": "cron",
        "expression": "0 2 * * *",
        "timezone": "UTC",
    }
    root = build_tree(
        tmp_path,
        sources=[make_source()],
        bindings=[bd],
    )
    registry = IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )
    ctx, history, _ch, _ibis = make_orchestrator_context(registry)
    history.processed_watch_keys_for_binding = lambda _b: set()
    watcher = Watcher(ctx, store_factory=lambda _src: InMemoryObjectStore())
    result = watcher.tick()
    assert result.bindings_inspected == 0
    assert result.loads_fired == 0


# ---------------------------------------------------------------------------
# Listing failure tolerance
# ---------------------------------------------------------------------------


def test_tick_continues_when_one_store_listing_fails(tmp_path: Path):
    """A broken store doesn't take down the whole tick."""
    ctx, _ = _make_ctx_with_watch_binding(tmp_path)

    class BrokenStore:
        kind = "broken"

        def list_keys(self, **_kw):
            raise RuntimeError("transient I/O")

        def get_bytes(self, key):  # pragma: no cover
            raise RuntimeError("not called")

    watcher = Watcher(ctx, store_factory=lambda _src: BrokenStore())
    result = watcher.tick()  # Does not raise.
    assert result.loads_fired == 0


# ---------------------------------------------------------------------------
# run_forever bounds via stop_after
# ---------------------------------------------------------------------------


def test_run_forever_runs_finite_when_stop_after_set(tmp_path: Path, monkeypatch):
    ctx, _ = _make_ctx_with_watch_binding(tmp_path)
    watcher = Watcher(ctx, store_factory=lambda _src: InMemoryObjectStore())

    # No-op sleep so the test is instant.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    tick_count = {"n": 0}

    def counting_tick():
        tick_count["n"] += 1
        return None

    monkeypatch.setattr(watcher, "tick", counting_tick)
    watcher.run_forever(0.001, stop_after=3)
    assert tick_count["n"] == 3


def test_column_inference_refuses_oversized_file(tmp_path: Path, monkeypatch):
    """Period inference fully materialises the file through a format reader;
    oversized drops are skipped instead of parsed (decompression/memory cap)."""
    import precis_mcp.ingestion.watcher as w
    from tests.factories.ingestion import build_tree, make_binding, make_source

    monkeypatch.setattr(w, "_MAX_INFERENCE_BYTES", 16)

    bd = make_binding(
        binding_id="manual_drop__gl",
        source="manual_drop",
        schedule_mode="watch",
    )
    bd["schedule"]["watch"] = {
        "file_glob": "*.csv",
        "period_from": "column",
        "period_column": "period",
    }
    root = build_tree(
        tmp_path,
        sources=[make_source(source_id="manual_drop", kind="http_upload")],
        bindings=[bd],
    )
    registry = IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )
    ctx, history, _ch, _ibis = make_orchestrator_context(registry)
    history.processed_watch_keys_for_binding = lambda binding_id: set()

    store = InMemoryObjectStore()
    store.put("test/gl_drop.csv", b"period,amount\n" + b"2026-04,1\n" * 10)

    watcher = Watcher(ctx, store_factory=lambda _src: store)
    result = watcher.tick()
    assert result.files_seen == 1
    assert result.loads_skipped_period_inference == 1
    assert result.loads_fired == 0
