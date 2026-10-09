# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Dataset lifetime, capacity and access isolation using the shared Redis fake."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from precis_mcp import datasets
from precis_mcp.auth import AuthContext, UserPermissions, clear_auth_context, set_auth_context
from tests.fakes.fake_redis import FakeRedis


def auth(user="alice", admin=True):
    set_auth_context(AuthContext(user_id=user, permissions=UserPermissions(user_id=user, is_admin=admin)))


@pytest.fixture
def store(monkeypatch):
    client = FakeRedis(decode_responses=True)
    monkeypatch.setattr(datasets, "get_redis_client", lambda: client)
    auth()
    yield client
    clear_auth_context()


def snapshot():
    return datasets.cache_read_result({"kind": "inspection", "rows": [{"amount": 1.23456789}]}, "inspect_rows", {"limit": 200})


def test_snapshot_is_immutable_and_preserves_precision(store):
    result = {"rows": [{"value": 1.23456789}]}
    ref = datasets.cache_read_result(result, "run_metric", {"scale": 3, "decimals": 1})
    result["rows"][0]["value"] = 0
    payload, error = datasets.fetch_cached_read_result("alice", ref)
    assert error is None
    assert payload["result"]["rows"][0]["value"] == 1.23456789
    assert payload["source"]["args"] == {"scale": 3, "decimals": 1}


def test_ref_cannot_cross_users_or_changed_grants(store):
    ref = snapshot()
    auth("bob")
    assert datasets.fetch_cached_read_result("bob", ref)[1]
    assert datasets.fetch_cached_read_result("alice", ref)[1]
    auth(admin=False)
    assert "permissions changed" in datasets.fetch_cached_read_result("alice", ref)[1]


def test_idle_renewal_never_crosses_absolute_expiry(store, monkeypatch):
    ref = snapshot()
    key = datasets._keys("alice", ref)[0]
    payload = json.loads(store.get(key))
    store.expire(key, 30)
    assert datasets.fetch_cached_read_result("alice", ref)[1] is None
    assert store.ttl(key) > 500
    monkeypatch.setattr(datasets.time, "time", lambda: payload["expires_at"] + 1)
    assert "expired" in datasets.fetch_cached_read_result("alice", ref)[1]


def test_concurrent_writers_cannot_exceed_owner_capacity(store, monkeypatch):
    monkeypatch.setenv("PRECIS_DATASET_USER_COUNT", "2")
    def write(_):
        auth()
        return snapshot()
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(write, range(8)))
    assert sum(ref is not None for ref in refs) == 2


def test_capacity_is_reclaimed_after_idle_expiry(store, monkeypatch):
    monkeypatch.setenv("PRECIS_DATASET_USER_COUNT", "1")
    first = snapshot()
    assert snapshot() is None
    key, index, _ = datasets._keys("alice", first)
    store.zadd(index, {key: 0})
    assert snapshot() is not None
    assert not store.exists(key)


@pytest.mark.parametrize("refs", ["ds_" + "0" * 32, "[\"ds_000\"]", [], ["legacy:ref"], [1]])
def test_chart_input_contract_rejects_coercions(refs):
    assert datasets.normalize_data_refs(refs) is None
