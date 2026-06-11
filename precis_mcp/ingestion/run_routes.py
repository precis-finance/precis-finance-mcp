# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Push API endpoint — `POST /api/ingest/run`.

Synchronously triggers `run_binding(binding_id, period, "push:<caller>")`
and returns the resulting `load_id` + terminal status. The caller (customer's
orchestrator, manual operator script, automated retry) holds the HTTP
connection open for the lifetime of the load.

Unlike the upload endpoint (which only persists bytes and lets the watcher
trigger the load asynchronously), the push endpoint is the synchronous
trigger path. It's intended for warehouse-direct sources where the extract
is a single fast SQL statement; long-running extracts will hold the
connection, which is the caller's concern.

Idempotency key honours the same 24-hour dedup window as the upload endpoint;
a repeat request with the same key returns the original `load_id` rather
than re-firing the load.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from precis_mcp.ingestion.orchestrator import OrchestratorContext, run_binding
from precis_mcp.ingestion.registry import IntegrationConfigError, IntegrationRegistry
from precis_mcp.ingestion.upload_routes import (
    IdempotencyCache,
    InMemoryIdempotencyCache,
    TokenVerifier,
)
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.run")


_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    """Body of POST /api/ingest/run.

    `override_lock` and `notes` exist for the admin re-trigger flow
    (see `docs/architecture/03-integration.md` "Current state" — Push API
    + historical reload): a deliberate, audit-trailed reload of a closed
    period. Both are recorded on `load_history` (notes verbatim, override_lock
    as part of the `triggered_by` audit suffix); the actual period-lock
    enforcement check itself is tracked separately and lands when the lock
    subsystem ships.
    """

    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1)
    # Canonical Précis period: 'YYYY-MM' for calendar months, or
    # adjustment forms like 'YYYY-13' / 'YYYY-MM-ADJ'. The loose pattern
    # matches the load_history CHECK relaxed in migration 026. Required
    # for `kind='period'` bindings; must be omitted for `kind='snapshot'`
    # bindings (master data has no period dimension) — enforced against
    # the binding's declared kind by `require_period_matches_kind`.
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-.+$")
    idempotency_key: Optional[str] = None
    override_lock: bool = False
    notes: Optional[str] = Field(default=None, max_length=2048)


# ---------------------------------------------------------------------------
# Idempotency record — track load_id per dedup key so retries return the
# original. Distinct from the upload endpoint's cache (uploads don't carry a
# load_id).
# ---------------------------------------------------------------------------


class IdempotencyLoadIdCache:
    """A trivial in-memory cache mapping `idempotency_key` → `(load_id,
    body_hash)`. Production deployments pass a Redis-backed implementation
    with the same shape (key, value, TTL).

    The cache stores `body_hash` so the endpoint can enforce the spec
    contract: "same key + same body returns the original load_id; same key
    + different body returns 409 Conflict." The hash covers the
    request's `(binding_id, period)` — `idempotency_key` itself is excluded
    because it's the cache key, not part of the payload-equality test.
    """

    def __init__(self) -> None:
        self._map: dict[str, tuple[str, str]] = {}

    def lookup(self, key: str) -> Optional[tuple[str, str]]:
        return self._map.get(key)

    def remember(self, key: str, load_id: str, body_hash: str, ttl_seconds: int) -> None:  # noqa: ARG002
        self._map[key] = (load_id, body_hash)


def require_period_matches_kind(
    registry: IntegrationRegistry,
    binding_id: str,
    period: Optional[str],
) -> None:
    """Reject a trigger whose `period` doesn't fit the binding's kind.

    Raises 404 for an unknown binding, 422 when a period binding is
    triggered without a period or a snapshot binding is triggered with
    one. Run after the token-scope gate so unauthorised callers can't
    probe binding existence.
    """
    try:
        binding = registry.get_binding(binding_id)
    except IntegrationConfigError:
        raise HTTPException(404, f"Unknown binding_id {binding_id!r}") from None
    if binding.kind == "period" and period is None:
        raise HTTPException(
            422,
            f"Binding {binding_id!r} is kind='period'; 'period' is required",
        )
    if binding.kind == "snapshot" and period is not None:
        raise HTTPException(
            422,
            f"Binding {binding_id!r} is kind='snapshot'; it has no period "
            "dimension — omit 'period'",
        )


def _hash_run_body(binding_id: str, period: Optional[str]) -> str:
    """Canonical hash of the request body's idempotency-relevant fields.

    Stable across the JSON serialisation choices of any client because we
    canonicalise: sorted key order, no whitespace, ASCII. SHA-256 truncated
    to 16 hex chars is enough collision-resistance for a 24h dedup window.
    """
    payload = json.dumps(
        {"binding_id": binding_id, "period": period},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_run_router(
    *,
    ctx: OrchestratorContext,
    token_verifier: TokenVerifier,
    idempotency_cache: Optional[IdempotencyLoadIdCache] = None,
    presence_cache: Optional[IdempotencyCache] = None,
) -> APIRouter:
    """Build the FastAPI router for `POST /api/ingest/run`.

    `ctx` — the production `OrchestratorContext` (PostgresLoadHistoryWriter
        + advisory lock + clickhouse-connect executor + driver registry).
    `token_verifier` — same shape as the upload endpoint; the JWT's
        `binding_id` claim must match the request body's `binding_id`.
    `idempotency_cache` — maps idempotency_key → load_id (24h TTL). When a
        repeat key arrives, the original load_id is returned without
        re-firing the load.
    `presence_cache` — optional secondary "seen this key recently" cache; if
        supplied, also writes the key (compatibility surface for sharing one
        cache instance with the upload endpoint).
    """
    cache = idempotency_cache or IdempotencyLoadIdCache()
    router = APIRouter()

    @router.post("/api/ingest/run", status_code=200)
    async def run(
        body: RunRequest,
        authorization: str = Header(...),
    ):
        # -- Auth ---------------------------------------------------------
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing Bearer token")
        raw_token = authorization[len("Bearer "):].strip()
        try:
            claims = token_verifier(raw_token)
        except Exception as exc:
            _logger.warning("ingest.run.auth_failed", error=str(exc))
            raise HTTPException(401, "Invalid token") from None

        # The token's binding_ids list is the authoritative scope; reject
        # when the request body asks for a binding outside that set.
        if body.binding_id not in claims.binding_ids:
            _logger.warning(
                "ingest.run.binding_not_authorised",
                token_binding_ids=list(claims.binding_ids),
                body_binding=body.binding_id,
            )
            raise HTTPException(
                403, "Token does not authorise this binding_id"
            )

        # -- Period × kind contract ----------------------------------------
        require_period_matches_kind(ctx.registry, body.binding_id, body.period)

        # -- Role gate (spec §11.3) --------------------------------------
        # Re-triggering a load requires admin or plan_manager. override_lock
        # additionally requires admin — bypassing a closed-period lock is an
        # admin-only deliberate audit-trailed action.
        roles = set(claims.roles)
        if not (roles & {"admin", "plan_manager"}):
            _logger.warning(
                "ingest.run.role_denied",
                caller=claims.caller,
                roles=list(claims.roles),
                binding_id=body.binding_id,
            )
            raise HTTPException(
                403, "Re-triggering a load requires admin or plan_manager role"
            )
        if body.override_lock and "admin" not in roles:
            _logger.warning(
                "ingest.run.override_lock_denied",
                caller=claims.caller,
                roles=list(claims.roles),
                binding_id=body.binding_id,
            )
            raise HTTPException(
                403, "override_lock requires admin role"
            )

        # -- Idempotency --------------------------------------------------
        # Per spec §8.3: same key + same body returns the original load_id;
        # same key + different body returns 409 Conflict. The body hash
        # covers (binding_id, period) — the idempotency_key itself is the
        # cache key, not part of the equality test.
        body_hash = _hash_run_body(body.binding_id, body.period)
        if body.idempotency_key:
            entry = cache.lookup(body.idempotency_key)
            if entry is not None:
                prior_load_id, prior_body_hash = entry
                if prior_body_hash != body_hash:
                    _logger.warning(
                        "ingest.run.idempotency_body_mismatch",
                        idempotency_key=body.idempotency_key,
                        prior_load_id=prior_load_id,
                        body_binding=body.binding_id,
                        body_period=body.period,
                    )
                    raise HTTPException(
                        409,
                        (
                            "Idempotency key reused with a different request "
                            "body. The key is already bound to load_id "
                            f"{prior_load_id!r}."
                        ),
                    )
                _logger.info(
                    "ingest.run.dedup_hit",
                    binding_id=body.binding_id,
                    idempotency_key=body.idempotency_key,
                    load_id=prior_load_id,
                )
                return {
                    "status": "deduplicated",
                    "binding_id": body.binding_id,
                    "period": body.period,
                    "idempotency_key": body.idempotency_key,
                    "load_id": prior_load_id,
                }
            if presence_cache is not None:
                presence_cache.remember(
                    body.idempotency_key, _IDEMPOTENCY_TTL_SECONDS
                )

        # -- Run ----------------------------------------------------------
        # `override_lock` is recorded in the audit suffix on `triggered_by`
        # so a forensic read of load_history shows the lock was deliberately
        # bypassed. The actual period-lock enforcement check itself is
        # deferred (cross-cut to the lock subsystem); today the flag is
        # purely audit-trailed.
        override_marker = "[override_lock]" if body.override_lock else ""
        triggered_by = f"push:{claims.caller}{override_marker}"
        _logger.info(
            "ingest.run.start",
            binding_id=body.binding_id,
            period=body.period,
            caller=claims.caller,
            override_lock=body.override_lock,
            has_notes=body.notes is not None,
        )
        try:
            attempt = run_binding(
                ctx,
                body.binding_id,
                body.period,
                triggered_by,
                notes=body.notes,
            )
        except Exception as exc:
            _logger.exception(
                "ingest.run.orchestrator_failed",
                binding_id=body.binding_id,
            )
            raise HTTPException(500, f"Orchestrator failure: {exc}") from None

        if body.idempotency_key:
            cache.remember(
                body.idempotency_key,
                attempt.load_id,
                body_hash,
                _IDEMPOTENCY_TTL_SECONDS,
            )

        # Map terminal status → HTTP code. Successful loads return 200;
        # blocking failures (recon, swap, extract) return 200 too with the
        # status in the body — the load completed in a well-defined terminal
        # state, the caller will inspect `status` to decide what to do.
        return {
            "status": attempt.status,
            "load_id": attempt.load_id,
            "binding_id": body.binding_id,
            "period": body.period,
            "rows_landed": attempt.rows_landed,
            "duration_ms": attempt.duration_ms,
            "error": attempt.error,
        }

    return router


__all__ = [
    "IdempotencyLoadIdCache",
    "InMemoryIdempotencyCache",
    "RunRequest",
    "build_run_router",
    "require_period_matches_kind",
]
