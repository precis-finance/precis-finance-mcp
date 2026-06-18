# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""HTTPS upload endpoint — `POST /api/ingest/upload`.

Receives a file payload from a customer's orchestrator (or a manual upload),
verifies a binding-scoped JWT, deduplicates via an idempotency key, persists
the file in the configured `LocalFsObjectStore` (or any `ObjectStoreClient`
that supports `.put()`), and returns `202 Accepted` with the stored key.

The watcher picks up the new file on its next tick and triggers `run_binding`.
The endpoint never invokes the orchestrator directly — keeping it fast,
non-blocking, and free of ClickHouse / Redis runtime dependencies.

Deployment wires this router into the main FastAPI app; tests use the
`build_upload_router(...)` factory directly with injected dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from fastapi import APIRouter, Header, HTTPException, UploadFile

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.upload")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class UploadStore(Protocol):
    """Subset of ObjectStoreClient that the endpoint writes to."""

    def put(self, key: str, data: bytes) -> None: ...


@dataclass(frozen=True)
class BindingScopedToken:
    """Decoded JWT claims relevant to ingest endpoints.

    `binding_ids` is the **set of bindings** the caller is authorised to
    target — one JWT can scope several bindings so a customer's orchestrator
    can use the same credential for both their GL push and their timesheets
    push. The request body (push) or `X-Binding-Id` header (upload) names
    which one of those bindings this specific call targets; the endpoint
    rejects with 403 if the requested id isn't in `binding_ids`.

    `roles` carries the caller's role names — used by the push endpoint to
    require `admin` or `plan_manager` for re-triggers, and `admin` for the
    `override_lock` path (see `deployment/security-model.md` for the role
    model). The upload endpoint
    does not consult `roles`: the binding-scoped JWT itself confers per-
    binding write authority, and customer orchestrators that push GL files
    typically run under a service identity that wouldn't carry analyst /
    planner / manager roles in the platform's sense.
    """

    binding_ids: tuple[str, ...]
    caller: str
    roles: tuple[str, ...] = ()


TokenVerifier = Callable[[str], BindingScopedToken]


class IdempotencyCache(Protocol):
    """Minimal Redis-shaped TTL set for dedup. Tests pass an in-memory fake."""

    def seen(self, key: str) -> bool: ...
    def remember(self, key: str, ttl_seconds: int) -> None: ...


class InMemoryIdempotencyCache:
    """Reference implementation used by tests."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def seen(self, key: str) -> bool:
        return key in self._keys

    def remember(self, key: str, ttl_seconds: int) -> None:  # noqa: ARG002
        self._keys.add(key)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


_MAX_UPLOAD_BYTES = 256 * 1024 * 1024  # 256 MiB — guard against runaway
_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def build_upload_router(
    *,
    token_verifier: TokenVerifier,
    upload_store: UploadStore,
    idempotency_cache: Optional[IdempotencyCache] = None,
    key_prefix: str = "uploads/",
    max_bytes: int = _MAX_UPLOAD_BYTES,
) -> APIRouter:
    """Build the FastAPI router. Dependencies are injected, no globals.

    `upload_store` writes the persisted file (typically a `LocalFsObjectStore`
    pointed at a path the watcher also reads).
    `token_verifier` decodes the binding-scoped JWT and returns the claims;
    raises any exception → 401.
    `idempotency_cache` dedups retries from the customer's orchestrator. If
    None, an in-memory cache is used (single-process only).
    """
    cache = idempotency_cache or InMemoryIdempotencyCache()
    router = APIRouter()

    @router.post("/api/ingest/upload", status_code=202)
    async def upload(
        file: UploadFile,
        authorization: str = Header(...),
        x_binding_id: str = Header(...),
        x_idempotency_key: Optional[str] = Header(default=None),
    ):
        # -- Auth ---------------------------------------------------------
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing Bearer token")
        raw_token = authorization[len("Bearer "):].strip()
        try:
            claims = token_verifier(raw_token)
        except Exception as exc:
            _logger.warning(
                "ingest.upload.auth_failed",
                error=str(exc),
            )
            raise HTTPException(401, "Invalid token") from None

        # -- Binding scope check -----------------------------------------
        binding_id = x_binding_id.strip()
        if not binding_id:
            raise HTTPException(400, "X-Binding-Id header required")
        if not _FILENAME_RE.match(binding_id):
            # binding_id becomes a path segment of the object-store key;
            # charset-validate it like the filename even though the value
            # is also cross-checked against the signed claim below.
            raise HTTPException(
                400,
                "binding_id must match [A-Za-z0-9._-]+ to keep object-store keys safe",
            )
        if binding_id not in claims.binding_ids:
            _logger.warning(
                "ingest.upload.binding_not_authorised",
                binding_id=binding_id,
                token_binding_ids=list(claims.binding_ids),
            )
            raise HTTPException(
                403, "Token does not authorise this binding_id"
            )

        # -- Filename hygiene --------------------------------------------
        if not file.filename or not _FILENAME_RE.match(file.filename):
            raise HTTPException(
                400,
                "filename must match [A-Za-z0-9._-]+ to keep object-store keys safe",
            )

        # -- Read payload + size guard -----------------------------------
        # Reject before materialising: the multipart spool's size is known
        # once parsed, and the chunked read below is the backstop when it
        # isn't — the full body must never be allocated just to measure it.
        if file.size is not None and file.size > max_bytes:
            raise HTTPException(
                413, f"Upload exceeds max size ({max_bytes} bytes)"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    413, f"Upload exceeds max size ({max_bytes} bytes)"
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            raise HTTPException(400, "Empty payload")

        # -- Idempotency --------------------------------------------------
        if x_idempotency_key:
            if cache.seen(x_idempotency_key):
                _logger.info(
                    "ingest.upload.dedup_hit",
                    binding_id=binding_id,
                    idempotency_key=x_idempotency_key,
                )
                # Same key + same body returns the original load_id. We
                # don't yet track per-key load_ids here; return 202 with a
                # dedup-acknowledged status.
                return {
                    "status": "deduplicated",
                    "binding_id": binding_id,
                    "idempotency_key": x_idempotency_key,
                }
            cache.remember(x_idempotency_key, _IDEMPOTENCY_TTL_SECONDS)

        # -- Persist ------------------------------------------------------
        key = f"{key_prefix}{binding_id}/{file.filename}"
        upload_store.put(key, body)
        _logger.info(
            "ingest.upload.persisted",
            binding_id=binding_id,
            key=key,
            bytes=len(body),
            caller=claims.caller,
        )
        return {
            "status": "accepted",
            "binding_id": binding_id,
            "stored_key": key,
            "bytes": len(body),
        }

    return router
