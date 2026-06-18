# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the helpers in `precis_mcp/ingestion/run_routes.py`.

The endpoint itself is exercised in `tests/component/test_run_endpoint.py`;
this module unit-tests the pure helpers — body-hash determinism and the
in-memory `IdempotencyLoadIdCache` — that the endpoint's contract
(same key + different body → 409 Conflict) relies on.
"""

from __future__ import annotations

from precis_mcp.ingestion.run_routes import (
    IdempotencyLoadIdCache,
    _hash_run_body,
)


# ---------------------------------------------------------------------------
# _hash_run_body — must be deterministic and discriminating
# ---------------------------------------------------------------------------


def test_hash_is_deterministic_for_same_inputs():
    h1 = _hash_run_body("manual_drop__gl", "2026-04")
    h2 = _hash_run_body("manual_drop__gl", "2026-04")
    assert h1 == h2


def test_hash_differs_when_period_changes():
    h_april = _hash_run_body("manual_drop__gl", "2026-04")
    h_may = _hash_run_body("manual_drop__gl", "2026-05")
    assert h_april != h_may


def test_hash_differs_when_binding_changes():
    h_gl = _hash_run_body("manual_drop__gl", "2026-04")
    h_ap = _hash_run_body("manual_drop__ap", "2026-04")
    assert h_gl != h_ap


def test_hash_is_stable_length():
    """Sixteen hex chars — narrows collision risk to 2^-64-ish within the
    24h dedup window, plenty for the use case."""
    assert len(_hash_run_body("a", "2026-04")) == 16


# ---------------------------------------------------------------------------
# IdempotencyLoadIdCache
# ---------------------------------------------------------------------------


def test_cache_lookup_misses_for_unknown_key():
    cache = IdempotencyLoadIdCache()
    assert cache.lookup("never-seen") is None


def test_cache_returns_load_id_and_body_hash_on_hit():
    cache = IdempotencyLoadIdCache()
    cache.remember("k1", "L1", "deadbeef00000000", 3600)
    entry = cache.lookup("k1")
    assert entry == ("L1", "deadbeef00000000")


def test_cache_remember_overwrites_existing_key():
    """The endpoint never re-remembers a key it found on lookup, but the
    underlying cache is permissive — last write wins."""
    cache = IdempotencyLoadIdCache()
    cache.remember("k1", "L1", "hash_a", 3600)
    cache.remember("k1", "L2", "hash_b", 3600)
    assert cache.lookup("k1") == ("L2", "hash_b")
