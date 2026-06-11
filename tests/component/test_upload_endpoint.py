# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the HTTPS upload endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from precis_mcp.ingestion.object_store import LocalFsObjectStore
from precis_mcp.ingestion.upload_routes import (
    BindingScopedToken,
    InMemoryIdempotencyCache,
    build_upload_router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_BINDING = "manual_drop__gl"


def _verifier_accepting(*binding_ids: str):
    ids = binding_ids or (_DEFAULT_BINDING,)

    def verify(token: str) -> BindingScopedToken:
        if not token:
            raise ValueError("empty token")
        return BindingScopedToken(binding_ids=ids, caller=f"user:{token[:8]}")

    return verify


def _verifier_rejecting():
    def verify(_token: str) -> BindingScopedToken:
        raise ValueError("bad token")

    return verify


def _headers(
    *,
    token: str = "t",
    binding_id: str | None = _DEFAULT_BINDING,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    out: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if binding_id is not None:
        out["X-Binding-Id"] = binding_id
    if idempotency_key is not None:
        out["X-Idempotency-Key"] = idempotency_key
    return out


def _client(tmp_path: Path, **kwargs):
    store = LocalFsObjectStore(tmp_path / "landing")
    router = build_upload_router(
        token_verifier=kwargs.get("verifier", _verifier_accepting()),
        upload_store=store,
        idempotency_cache=kwargs.get("cache"),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), store


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_upload_accepted_persists_file(tmp_path: Path):
    client, store = _client(tmp_path)
    payload = b"Date,Account\n2026-04-01,1000\n"
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", payload, "text/csv")},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["stored_key"] == "uploads/manual_drop__gl/gl_2026-04.csv"
    assert body["bytes"] == len(payload)
    assert store.get_bytes("uploads/manual_drop__gl/gl_2026-04.csv") == payload


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_upload_missing_authorization_header_rejected(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
    )
    # FastAPI raises 422 when a required Header(...) parameter is absent.
    assert resp.status_code == 422


def test_upload_non_bearer_scheme_rejected(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
        headers={"Authorization": "Basic something", "X-Binding-Id": _DEFAULT_BINDING},
    )
    assert resp.status_code == 401


def test_upload_invalid_token_rejected(tmp_path: Path):
    client, _ = _client(tmp_path, verifier=_verifier_rejecting())
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
        headers=_headers(),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Binding scope (X-Binding-Id)
# ---------------------------------------------------------------------------


def test_upload_missing_x_binding_id_header_rejected(tmp_path: Path):
    """No `X-Binding-Id` header → 422 (FastAPI required-header check)."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 422


def test_upload_binding_outside_token_scope_rejected(tmp_path: Path):
    """X-Binding-Id naming a binding NOT in the token's binding_ids → 403."""
    client, _ = _client(
        tmp_path,
        verifier=_verifier_accepting("manual_drop__gl"),  # token only has this one
    )
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
        headers=_headers(binding_id="other_binding"),
    )
    assert resp.status_code == 403


def test_upload_one_token_two_bindings(tmp_path: Path):
    """A token with `binding_ids=(a, b)` can target either via X-Binding-Id."""
    client, store = _client(
        tmp_path,
        verifier=_verifier_accepting("alpha__gl", "beta__gl"),
    )
    for binding_id in ("alpha__gl", "beta__gl"):
        resp = client.post(
            "/api/ingest/upload",
            files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
            headers=_headers(binding_id=binding_id),
        )
        assert resp.status_code == 202, binding_id
        assert store.get_bytes(f"uploads/{binding_id}/gl_2026-04.csv") == b"x"


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "../etc/passwd",
        "evil/path.csv",
        "with space.csv",
        "weird;chars.csv",
    ],
)
def test_upload_rejects_unsafe_filename(tmp_path: Path, filename: str):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": (filename, b"x", "text/csv")},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "filename" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Size and emptiness
# ---------------------------------------------------------------------------


def test_upload_rejects_empty_payload(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"", "text/csv")},
        headers=_headers(),
    )
    assert resp.status_code == 400


def test_upload_rejects_oversized_payload(tmp_path: Path):
    store = LocalFsObjectStore(tmp_path / "landing")
    router = build_upload_router(
        token_verifier=_verifier_accepting(),
        upload_store=store,
        max_bytes=10,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x" * 100, "text/csv")},
        headers=_headers(),
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_upload_dedups_via_idempotency_key(tmp_path: Path):
    cache = InMemoryIdempotencyCache()
    client, store = _client(tmp_path, cache=cache)

    first = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"original", "text/csv")},
        headers=_headers(idempotency_key="abc-123"),
    )
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    second = client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"different", "text/csv")},
        headers=_headers(idempotency_key="abc-123"),
    )
    assert second.status_code == 202
    assert second.json()["status"] == "deduplicated"
    assert store.get_bytes("uploads/manual_drop__gl/gl_2026-04.csv") == b"original"


def test_upload_without_idempotency_key_writes_each_request(tmp_path: Path):
    """Two requests with no key are both processed; latest write wins."""
    client, store = _client(tmp_path)
    client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"first", "text/csv")},
        headers=_headers(),
    )
    client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"second", "text/csv")},
        headers=_headers(),
    )
    assert store.get_bytes("uploads/manual_drop__gl/gl_2026-04.csv") == b"second"


# ---------------------------------------------------------------------------
# Per-binding scoping
# ---------------------------------------------------------------------------


def test_upload_scoping_writes_under_binding_id(tmp_path: Path):
    """The persisted key includes the binding_id, so two bindings can't collide."""
    client, store = _client(tmp_path, verifier=_verifier_accepting("billing__gl"))
    client.post(
        "/api/ingest/upload",
        files={"file": ("gl_2026-04.csv", b"x", "text/csv")},
        headers=_headers(binding_id="billing__gl"),
    )
    assert store.get_bytes("uploads/billing__gl/gl_2026-04.csv") == b"x"


@pytest.mark.parametrize("binding_id", ["../escape", "a/b", "evil\\x", "a..%2Fb "])
def test_upload_rejects_unsafe_binding_id(tmp_path: Path, binding_id: str):
    """binding_id becomes an object-store path segment — charset-validated
    like the filename, even though it is also checked against the claim."""
    client, store = _client(
        tmp_path, verifier=_verifier_accepting(binding_id),
    )
    resp = client.post(
        "/api/ingest/upload",
        headers=_headers(binding_id=binding_id),
        files={"file": ("gl.csv", b"period\n2026-04\n")},
    )
    assert resp.status_code == 400
    assert "binding_id" in resp.json()["detail"]


def test_upload_oversized_rejected_without_full_materialisation(tmp_path: Path):
    """The cap must reject before the whole body is joined into one bytes
    object (cap checked per chunk / via the spool size)."""
    store = LocalFsObjectStore(tmp_path / "landing")
    router = build_upload_router(
        token_verifier=_verifier_accepting(),
        upload_store=store,
        max_bytes=1024,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post(
        "/api/ingest/upload",
        headers=_headers(),
        files={"file": ("gl.csv", b"x" * 4096)},
    )
    assert resp.status_code == 413
    assert not list((tmp_path / "landing").rglob("*.csv"))
