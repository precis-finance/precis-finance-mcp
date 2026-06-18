# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the push API endpoint `POST /api/ingest/run`.

Synchronously triggers `run_binding` and returns the terminal load_id +
status. Verifies the auth + binding-scope gate, the body-hash idempotency
(same key + different body → 409 Conflict), and the orchestrator
failure path.

Component-class: in-process FastAPI TestClient + the shared ingestion
fakes; no real services.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.run_routes import build_run_router
from precis_mcp.ingestion.upload_routes import BindingScopedToken
from tests.factories.ingestion import (
    build_tree,
    make_binding,
    make_orchestrator_context,
    make_source,
)
from tests.fakes.ingestion import FakeIbisBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BINDING = "manual_drop__gl"
_SNAPSHOT_BINDING = "manual_drop__accounts"


def _verifier(*binding_ids: str, roles: tuple[str, ...] = ("admin",)):
    """Build a token verifier for tests.

    `roles` defaults to `("admin",)` so happy-path tests pass the role
    gate without each test having to thread roles in. Role-gate tests pass
    explicit role sets (including the empty tuple).
    """
    ids = binding_ids or (_BINDING, _SNAPSHOT_BINDING)

    def verify(token: str) -> BindingScopedToken:
        if not token:
            raise ValueError("empty token")
        return BindingScopedToken(
            binding_ids=ids, caller=f"user:{token[:8]}", roles=roles
        )

    return verify


def _registry(tmp_path: Path) -> IntegrationRegistry:
    src = make_source()
    bd = make_binding(binding_id=_BINDING)
    bd["schedule"] = {
        "mode": "push",
        "push_auth": {"role": "ingest_runner", "binding_token_ref": "t"},
    }
    snap = make_binding(
        binding_id=_SNAPSHOT_BINDING,
        target="live.dim_account",
        kind="snapshot",
    )
    root = build_tree(tmp_path, sources=[src], bindings=[bd, snap])
    return IntegrationRegistry.load(
        root, secret_check_env={"MANUAL_DROP_SECRET_KEY": "x"}
    )


def _client(
    tmp_path: Path,
    *,
    binding_ids=None,
    driver_exc: Exception | None = None,
    roles: tuple[str, ...] = ("admin",),
):
    """Build a FastAPI app with the run router mounted against fakes."""
    registry = _registry(tmp_path)
    ibis_backend = FakeIbisBackend(exc=driver_exc) if driver_exc else None
    ctx, history, _ch, _ibis = make_orchestrator_context(
        registry, ibis_backend=ibis_backend,
    )
    verifier = _verifier(*(binding_ids or ()), roles=roles)
    router = build_run_router(ctx=ctx, token_verifier=verifier)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), history


def _headers(token: str = "t") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_endpoint_happy_path_returns_success_and_load_id(tmp_path: Path):
    client, history = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["binding_id"] == _BINDING
    assert body["period"] == "2026-04"
    assert body["load_id"] in history.rows


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_run_endpoint_rejects_missing_authorization(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
    )
    assert resp.status_code == 422  # FastAPI required-header check


def test_run_endpoint_rejects_non_bearer_scheme(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers={"Authorization": "Basic x"},
    )
    assert resp.status_code == 401


def test_run_endpoint_rejects_invalid_token(tmp_path: Path):
    """Empty token causes the verifier to raise → 401."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Binding scope
# ---------------------------------------------------------------------------


def test_run_endpoint_403_when_body_binding_not_in_token(tmp_path: Path):
    """Token authorises X but body requests Y — endpoint returns 403."""
    client, _ = _client(tmp_path, binding_ids=("other_binding",))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 403


def test_run_endpoint_one_token_two_bindings(tmp_path: Path):
    """Plural token claim — request body picks one of the authorised ids."""
    client, _ = _client(tmp_path, binding_ids=(_BINDING, "other_binding"))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Body shape validation
# ---------------------------------------------------------------------------


def test_run_endpoint_rejects_missing_period_for_period_binding(tmp_path: Path):
    """`period` is optional at the model layer (snapshot bindings omit it)
    but required by the kind gate for `kind='period'` bindings."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "kind='period'" in resp.json()["detail"]


@pytest.mark.parametrize("bad", ["abc", "20260", "0000000"])
def test_run_endpoint_rejects_malformed_period(tmp_path: Path, bad: str):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": bad},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_run_endpoint_rejects_extra_body_fields(tmp_path: Path):
    """RunRequest has extra='forbid'; unknown fields are rejected."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "ghost_field": "boom",
        },
        headers=_headers(),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Period × kind contract — snapshot bindings have no period dimension
# ---------------------------------------------------------------------------


def test_run_endpoint_snapshot_binding_without_period_succeeds(tmp_path: Path):
    """Snapshot bindings (master data) are triggered with no `period`."""
    client, history = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _SNAPSHOT_BINDING},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["period"] is None
    assert body["load_id"] in history.rows


def test_run_endpoint_rejects_period_on_snapshot_binding(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _SNAPSHOT_BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "kind='snapshot'" in resp.json()["detail"]


def test_run_endpoint_404_for_unknown_binding_in_token_scope(tmp_path: Path):
    """A token may carry a binding id the registry doesn't know (config
    drift); the kind gate surfaces it as 404 rather than a 500 from the
    orchestrator."""
    client, _ = _client(tmp_path, binding_ids=("ghost_binding",))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": "ghost_binding", "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_run_endpoint_snapshot_idempotency_dedups_without_period(tmp_path: Path):
    """The idempotency body hash covers (binding_id, period=None) for
    snapshot bindings — a retry with the same key returns the original
    load_id."""
    client, _ = _client(tmp_path)
    first = client.post(
        "/api/ingest/run",
        json={"binding_id": _SNAPSHOT_BINDING, "idempotency_key": "snap-1"},
        headers=_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        "/api/ingest/run",
        json={"binding_id": _SNAPSHOT_BINDING, "idempotency_key": "snap-1"},
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"
    assert second.json()["load_id"] == first.json()["load_id"]


# ---------------------------------------------------------------------------
# Idempotency (body-hash check)
# ---------------------------------------------------------------------------


def test_run_endpoint_repeat_key_same_body_returns_original_load_id(tmp_path: Path):
    client, _ = _client(tmp_path)
    first = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "idempotency_key": "abc-123",
        },
        headers=_headers(),
    )
    assert first.status_code == 200
    original_load_id = first.json()["load_id"]

    second = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "idempotency_key": "abc-123",
        },
        headers=_headers(),
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "deduplicated"
    assert body["load_id"] == original_load_id


def test_run_endpoint_repeat_key_different_body_returns_409(tmp_path: Path):
    """Reusing an idempotency_key with a different body must return 409
    Conflict so a caller bug isn't masked by the wrong load_id."""
    client, _ = _client(tmp_path)
    first = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "idempotency_key": "abc-123",
        },
        headers=_headers(),
    )
    assert first.status_code == 200

    second = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-05",  # different period → different body hash
            "idempotency_key": "abc-123",
        },
        headers=_headers(),
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert "Idempotency key" in detail
    assert first.json()["load_id"] in detail


def test_run_endpoint_no_idempotency_key_runs_each_request(tmp_path: Path):
    """Without an idempotency_key, each request is its own attempt."""
    client, history = _client(tmp_path)
    r1 = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    r2 = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["load_id"] != r2.json()["load_id"]
    assert len(history.rows) == 2


# ---------------------------------------------------------------------------
# Orchestrator failure path
# ---------------------------------------------------------------------------


def test_run_endpoint_returns_terminal_failed_status_when_extract_fails(
    tmp_path: Path,
):
    """An extract-stage failure is a terminal load status, not a 500 — the
    load completed in a defined state, so the endpoint returns 200 with
    `status=failed_extract` and the caller decides what to do."""
    client, history = _client(
        tmp_path, driver_exc=RuntimeError("vendor api down")
    )
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed_extract"
    assert "vendor api down" in (body.get("error") or "")
    # load_history row exists with the same terminal status.
    assert history.rows[body["load_id"]]["status"] == "failed_extract"


# ---------------------------------------------------------------------------
# override_lock + notes (admin re-trigger audit fields)
# ---------------------------------------------------------------------------


def test_run_endpoint_records_notes_on_load_history(tmp_path: Path):
    """Operator-supplied `notes` are stored verbatim on the load_history row
    for the audit trail."""
    client, history = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "notes": "Reload after correcting GHOST_99 master-data entry",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    row = history.rows[resp.json()["load_id"]]
    assert row["notes"] == "Reload after correcting GHOST_99 master-data entry"


def test_run_endpoint_records_override_lock_in_triggered_by(tmp_path: Path):
    """`override_lock=True` appends an audit suffix to triggered_by so a
    forensic read of load_history shows the lock was deliberately bypassed."""
    client, history = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "override_lock": True,
            "notes": "Emergency reload — Q1 close adjustment",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    row = history.rows[resp.json()["load_id"]]
    assert "[override_lock]" in row["triggered_by"]
    assert row["notes"].startswith("Emergency reload")


def test_run_endpoint_override_lock_defaults_to_false(tmp_path: Path):
    """Absent `override_lock` → no audit suffix on triggered_by."""
    client, history = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    row = history.rows[resp.json()["load_id"]]
    assert "[override_lock]" not in row["triggered_by"]


def test_run_endpoint_notes_max_length_enforced(tmp_path: Path):
    """`notes` is capped at 2048 chars to keep load_history sane."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "notes": "x" * 2049,
        },
        headers=_headers(),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Role gate: re-trigger requires admin or plan_manager;
# override_lock additionally requires admin.
# ---------------------------------------------------------------------------


def test_run_endpoint_403_when_token_has_no_roles(tmp_path: Path):
    """Empty roles claim → 403 even when the binding scope matches."""
    client, _ = _client(tmp_path, roles=())
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert "admin or plan_manager" in resp.json()["detail"]


def test_run_endpoint_403_when_only_analyst_role(tmp_path: Path):
    """Analyst role does not authorise re-triggering a load."""
    client, _ = _client(tmp_path, roles=("analyst",))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 403


def test_run_endpoint_plan_manager_can_retrigger(tmp_path: Path):
    """plan_manager passes the re-trigger gate (without override_lock)."""
    client, _ = _client(tmp_path, roles=("plan_manager",))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 200


def test_run_endpoint_plan_manager_cannot_override_lock(tmp_path: Path):
    """override_lock requires admin; plan_manager alone is not enough."""
    client, _ = _client(tmp_path, roles=("plan_manager",))
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "override_lock": True,
        },
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert "override_lock" in resp.json()["detail"]


def test_run_endpoint_admin_can_override_lock(tmp_path: Path):
    """admin passes both the re-trigger gate and the override_lock gate."""
    client, history = _client(tmp_path, roles=("admin",))
    resp = client.post(
        "/api/ingest/run",
        json={
            "binding_id": _BINDING,
            "period": "2026-04",
            "override_lock": True,
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    row = history.rows[resp.json()["load_id"]]
    assert "[override_lock]" in row["triggered_by"]


def test_run_endpoint_admin_alone_can_retrigger(tmp_path: Path):
    """admin without override_lock is the canonical re-trigger path."""
    client, _ = _client(tmp_path, roles=("admin",))
    resp = client.post(
        "/api/ingest/run",
        json={"binding_id": _BINDING, "period": "2026-04"},
        headers=_headers(),
    )
    assert resp.status_code == 200
