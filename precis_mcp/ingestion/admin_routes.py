# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Admin HTTP surface for the ingestion subsystem on the API process.

HTTP surface for the ingestion admin UI. Routes mounted under
`/api/admin/`:

  - `POST /api/admin/reload_integrations` — refresh the API's
    `IntegrationRegistryRef` and clear the Ibis connection cache so
    federated reads pick up Source / credential changes without a restart.
  - `GET  /api/admin/sources`, `/sources/{id}` — list / inspect Sources.
  - `POST /api/admin/sources`, `PATCH …/{id}`, `DELETE …/{id}` —
    create / replace / delete a Source. Validate → cross-check against
    the live registry → atomic YAML write to
    `<integrations_root>/sources/<id>.yml` → reload → audit row.
  - `GET / POST / PATCH / DELETE /api/admin/bindings[/{id}]` — same shape,
    plus source FK pre-check.
  - `GET /api/admin/load_history` — paginated, filterable list.

Every endpoint declares `Depends(require_admin)` — same dependency the
rest of the admin surface uses. The JWT middleware (`JWTAuthMiddleware`
on the API process) decodes the token and stamps
`request.state.permissions` from the platform DB; the dependency reads
`is_admin` from there. No JWT-claim role checks, no parallel verifier
chain.

Audit rows go straight to `security_audit_log` via `execute_platform`,
matching the admin-surface audit pattern.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from precis_mcp.ingestion.registry import (
    Binding,
    IntegrationConfigError,
    Source,
)
from precis_mcp.observability import get_logger
from precis_mcp.http_auth import require_admin

if TYPE_CHECKING:
    from precis_mcp.ingestion.orchestrator import OrchestratorContext
    from precis_mcp.ingestion.wiring import IntegrationRegistryRef


_logger = get_logger("ingestion.admin")


def build_admin_router(
    *,
    registry_ref: "IntegrationRegistryRef",
    orchestrator_ctx: Optional["OrchestratorContext"] = None,
) -> APIRouter:
    """Build the FastAPI router for `/api/admin/*` ingestion routes.

    `registry_ref` — the live host-side `IntegrationRegistryRef` constructed
    by `attach_ingestion_to_app`. Reads project from `.current`; writes
    rewrite the underlying YAML files and call `.reload()`.

    The router relies on `JWTAuthMiddleware` (mounted on the API app) to
    populate `request.state.permissions` and `request.state.user_id`;
    `Depends(require_admin)` enforces the admin gate.
    """
    router = APIRouter()

    # -- Reload -----------------------------------------------------------

    @router.post(
        "/api/admin/reload_integrations",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def reload_integrations(request: Request):
        caller = request.state.user_id
        try:
            summary = registry_ref.reload()
        except Exception as exc:
            _logger.exception(
                "ingest.admin.reload_integrations.validation_failed",
                caller=caller,
            )
            raise HTTPException(400, f"Reload failed: {exc}") from None

        # Re-bind the IbisRegistry so federated reads pick up changes.
        from precis_mcp.engine.ibis_registry import set_integration_registry

        set_integration_registry(registry_ref)

        _logger.info(
            "ingest.admin.reload_integrations.succeeded",
            caller=caller,
            summary=summary,
        )
        return {"status": "reloaded", "summary": summary, "caller": caller}

    # -- Schema runners — apply DDL / refresh views from instance/ --------
    #
    # Both endpoints re-run the corresponding runner against the live
    # ClickHouse client. The DDL runner is idempotent
    # (`CREATE TABLE IF NOT EXISTS`); the semantic runner is fully
    # idempotent via `CREATE OR REPLACE VIEW`. Use these after editing
    # `instance/live/*.sql` or `instance/semantic/{dims,views}/*.sql`
    # respectively to make the changes live without restarting the
    # server.

    @router.post(
        "/api/admin/reload_live_ddl",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def reload_live_ddl(request: Request):
        from precis_mcp.db import get_clickhouse_client
        from precis_mcp.ingestion.live_ddl_runner import apply_all

        caller = request.state.user_id
        instance_root = registry_ref.root.parent  # <repo>/instance
        try:
            report = apply_all(instance_root / "live", get_clickhouse_client())
        except Exception as exc:
            _logger.exception(
                "ingest.admin.reload_live_ddl.failed", caller=caller
            )
            raise HTTPException(500, f"Apply failed: {exc}") from None

        _audit(
            caller, "ingest.live_ddl.reloaded", None,
            {
                "schemas_ensured": list(report.schemas_ensured),
                "tables_applied": [
                    f"{schema}.{name}" for schema, name in report.tables_applied
                ],
            },
        )
        _logger.info(
            "ingest.admin.reload_live_ddl.succeeded",
            caller=caller,
            tables_applied=len(report.tables_applied),
        )
        return {
            "status": "applied",
            "schemas_ensured": list(report.schemas_ensured),
            "tables_applied": [
                {"schema": schema, "table": name}
                for schema, name in report.tables_applied
            ],
        }

    @router.post(
        "/api/admin/reload_semantic_views",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def reload_semantic_views(request: Request):
        from precis_mcp.db import get_clickhouse_client
        from precis_mcp.engine import load_and_validate
        from precis_mcp.ingestion.semantic_runner import apply_all

        caller = request.state.user_id
        instance_root = registry_ref.root.parent  # <repo>/instance
        try:
            catalogue = load_and_validate(str(instance_root / "catalogue"))
            report = apply_all(
                instance_root / "semantic",
                get_clickhouse_client(),
                catalogue=catalogue,
            )
        except Exception as exc:
            _logger.exception(
                "ingest.admin.reload_semantic_views.failed", caller=caller
            )
            raise HTTPException(500, f"Apply failed: {exc}") from None

        _audit(
            caller, "ingest.semantic_views.reloaded", None,
            {"views_applied": list(report.views_applied)},
        )
        _logger.info(
            "ingest.admin.reload_semantic_views.succeeded",
            caller=caller,
            views_applied=len(report.views_applied),
        )
        return {
            "status": "applied",
            "views_applied": list(report.views_applied),
        }

    # Schemas — served by the admin surface under
    # `GET /api/admin/schemas/{name}` for all admin models (Source / Binding
    # / Profile). Single registry so the URL space stays clean.

    # -- Sources (read) ---------------------------------------------------

    @router.get(
        "/api/admin/sources",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def list_sources():
        items = [src.model_dump() for src in registry_ref.current.sources.values()]
        return {"items": items, "count": len(items)}

    @router.get(
        "/api/admin/sources/{source_id}",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def get_source(source_id: str):
        src = registry_ref.current.sources.get(source_id)
        if src is None:
            raise HTTPException(404, f"Source {source_id!r} not found")
        return src.model_dump()

    # -- Sources (write) --------------------------------------------------

    @router.post(
        "/api/admin/sources",
        status_code=201,
        dependencies=[Depends(require_admin)],
    )
    async def create_source(body: dict, request: Request):
        caller = request.state.user_id
        try:
            source = Source.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None

        if source.id in registry_ref.current.sources:
            raise HTTPException(
                409, f"Source {source.id!r} already exists; use PATCH to update"
            )

        path = registry_ref.root / "sources" / f"{source.id}.yml"
        _write_entity_yaml(path, source.model_dump())
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="source.create",
        )
        _audit(
            caller, "ingest.source.created", source.id,
            {"display_name": source.display_name, "kind": source.kind},
        )
        return source.model_dump()

    @router.patch(
        "/api/admin/sources/{source_id}",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def update_source(source_id: str, body: dict, request: Request):
        caller = request.state.user_id

        if source_id not in registry_ref.current.sources:
            raise HTTPException(404, f"Source {source_id!r} not found")

        body_with_id = dict(body)
        body_id = body_with_id.setdefault("id", source_id)
        if body_id != source_id:
            raise HTTPException(
                400,
                f"Body id {body_id!r} does not match path id {source_id!r}; "
                "rename via DELETE + POST",
            )

        try:
            source = Source.model_validate(body_with_id)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None

        path = registry_ref.root / "sources" / f"{source.id}.yml"
        previous_bytes = path.read_bytes() if path.exists() else None
        _write_entity_yaml(path, source.model_dump())
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="source.update", rollback_bytes=previous_bytes,
        )
        _audit(
            caller, "ingest.source.updated", source.id,
            {"display_name": source.display_name, "kind": source.kind},
        )
        return source.model_dump()

    @router.delete(
        "/api/admin/sources/{source_id}",
        status_code=204,
        dependencies=[Depends(require_admin)],
    )
    async def delete_source(source_id: str, request: Request):
        caller = request.state.user_id

        if source_id not in registry_ref.current.sources:
            raise HTTPException(404, f"Source {source_id!r} not found")

        referencing = registry_ref.current.bindings_for_source(source_id)
        if referencing:
            raise HTTPException(
                409,
                f"Source {source_id!r} is referenced by "
                f"{len(referencing)} binding(s): "
                f"{[b.id for b in referencing]}. Delete those bindings first.",
            )

        path = registry_ref.root / "sources" / f"{source_id}.yml"
        if path.exists():
            path.unlink()
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="source.delete",
        )
        _audit(caller, "ingest.source.deleted", source_id, {})
        return None

    # To onboard a new binding: author `instance/live/<table>.sql`
    # (column shape + engine spec for live + staging tables), apply via
    # the live-DDL runner, then POST a Binding here with
    # target=`live.<table>` + the operator-authored `extract.query`.

    # -- Bindings (read) --------------------------------------------------

    @router.get(
        "/api/admin/bindings",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def list_bindings(
        source_id: Optional[str] = Query(default=None),
        target: Optional[str] = Query(default=None),
    ):
        bindings = registry_ref.current.bindings.values()
        if source_id is not None:
            bindings = [b for b in bindings if b.source == source_id]
        if target is not None:
            bindings = [b for b in bindings if b.target == target]
        items = [b.model_dump() for b in bindings]
        return {"items": items, "count": len(items)}

    @router.get(
        "/api/admin/bindings/{binding_id}",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def get_binding(binding_id: str):
        bd = registry_ref.current.bindings.get(binding_id)
        if bd is None:
            raise HTTPException(404, f"Binding {binding_id!r} not found")
        return bd.model_dump()

    # -- Bindings (write) -------------------------------------------------
    #
    # Bindings carry an FK to Source and declare a `target` live table.
    # The endpoint pre-checks source existence so the common caller-error
    # case fails fast with 422 before we touch disk; deeper invariants
    # (target uniqueness across bindings) surface through the
    # reload-with-rollback path below.

    @router.post(
        "/api/admin/bindings",
        status_code=201,
        dependencies=[Depends(require_admin)],
    )
    async def create_binding(body: dict, request: Request):
        caller = request.state.user_id
        try:
            binding = Binding.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None

        if binding.id in registry_ref.current.bindings:
            raise HTTPException(
                409,
                f"Binding {binding.id!r} already exists; use PATCH to update",
            )
        _check_binding_fks(binding, registry_ref.current)

        path = registry_ref.root / "bindings" / f"{binding.id}.yml"
        _write_entity_yaml(path, binding.model_dump())
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="binding.create",
        )
        _audit(
            caller, "ingest.binding.created", binding.id,
            {"source": binding.source, "target": binding.target},
        )
        return binding.model_dump()

    @router.patch(
        "/api/admin/bindings/{binding_id}",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def update_binding(binding_id: str, body: dict, request: Request):
        caller = request.state.user_id

        if binding_id not in registry_ref.current.bindings:
            raise HTTPException(404, f"Binding {binding_id!r} not found")

        body_with_id = dict(body)
        body_id = body_with_id.setdefault("id", binding_id)
        if body_id != binding_id:
            raise HTTPException(
                400,
                f"Body id {body_id!r} does not match path id {binding_id!r}; "
                "rename via DELETE + POST",
            )

        try:
            binding = Binding.model_validate(body_with_id)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None
        _check_binding_fks(binding, registry_ref.current)

        path = registry_ref.root / "bindings" / f"{binding.id}.yml"
        previous_bytes = path.read_bytes() if path.exists() else None
        _write_entity_yaml(path, binding.model_dump())
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="binding.update", rollback_bytes=previous_bytes,
        )
        _audit(
            caller, "ingest.binding.updated", binding.id,
            {"source": binding.source, "target": binding.target},
        )
        return binding.model_dump()

    @router.delete(
        "/api/admin/bindings/{binding_id}",
        status_code=204,
        dependencies=[Depends(require_admin)],
    )
    async def delete_binding(binding_id: str, request: Request):
        caller = request.state.user_id

        if binding_id not in registry_ref.current.bindings:
            raise HTTPException(404, f"Binding {binding_id!r} not found")

        path = registry_ref.root / "bindings" / f"{binding_id}.yml"
        if path.exists():
            path.unlink()
        _safe_reload_with_rollback(
            registry_ref, rollback_path=path, caller=caller,
            action="binding.delete",
        )
        _audit(caller, "ingest.binding.deleted", binding_id, {})
        return None

    # -- Load history (read) ----------------------------------------------

    @router.get(
        "/api/admin/load_history",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def get_load_history(
        binding_id: Optional[str] = Query(default=None),
        dataset_id: Optional[str] = Query(default=None),
        period: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        from precis_mcp.ingestion.load_history import query_load_history

        rows = query_load_history(
            binding_id=binding_id,
            dataset_id=dataset_id,
            period=period,
            status=status,
            limit=limit,
        )
        return {"items": rows, "count": len(rows)}

    # -- Manual trigger (admin re-trigger from the UI) ---------------------
    #
    # Mirrors `POST /api/ingest/run` but admin-gated (require_admin) instead
    # of binding-scoped JWT — admin users have no `binding_ids` claim, so
    # the customer-orchestrator push endpoint rejects them. Calls
    # `run_binding` synchronously; the caller holds the connection for the
    # lifetime of the load.

    @router.post(
        "/api/admin/trigger_load",
        status_code=200,
        dependencies=[Depends(require_admin)],
    )
    async def trigger_load(body: dict, request: Request):
        if orchestrator_ctx is None:
            raise HTTPException(
                503,
                "Manual trigger requires the orchestrator context. Set "
                "PRECIS_ENABLE_PUSH_ENDPOINT=1 on the API host to wire it.",
            )

        from precis_mcp.ingestion.orchestrator import run_binding
        from precis_mcp.ingestion.run_routes import (
            RunRequest,
            require_period_matches_kind,
        )

        try:
            req = RunRequest.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from None

        require_period_matches_kind(
            orchestrator_ctx.registry, req.binding_id, req.period
        )

        caller = request.state.user_id
        override_marker = "[override_lock]" if req.override_lock else ""
        triggered_by = f"admin:{caller}{override_marker}"
        _logger.info(
            "ingest.admin.trigger_load.start",
            caller=caller,
            binding_id=req.binding_id,
            period=req.period,
            override_lock=req.override_lock,
            has_notes=req.notes is not None,
        )
        try:
            attempt = run_binding(
                orchestrator_ctx,
                req.binding_id,
                req.period,
                triggered_by,
                notes=req.notes,
            )
        except Exception as exc:
            _logger.exception(
                "ingest.admin.trigger_load.orchestrator_failed",
                caller=caller,
                binding_id=req.binding_id,
            )
            raise HTTPException(500, f"Orchestrator failure: {exc}") from None

        return {
            "status": attempt.status,
            "load_id": attempt.load_id,
            "binding_id": req.binding_id,
            "period": req.period,
            "rows_landed": attempt.rows_landed,
            "duration_ms": attempt.duration_ms,
            "error": attempt.error,
        }

    return router


# ---------------------------------------------------------------------------
# YAML persistence
# ---------------------------------------------------------------------------


def _write_entity_yaml(path: Path, payload: dict) -> None:
    """Atomically write a Source / Dataset / Binding's YAML to `path`.

    Uses tempfile-in-same-directory + `os.rename` so a partial write
    cannot leave the file half-populated even on crash. Existing readers
    (`IntegrationRegistry.load`) see either the old file or the new one,
    never a mid-write state.

    `pydantic.BaseModel.model_dump()` produces a dict that round-trips
    cleanly back through `model_validate`. yaml.safe_dump emits keys in
    insertion order — pydantic preserves schema order, so the on-disk YAML
    matches the declared field order.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    entity_id = payload.get("id", "entity")
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{entity_id}.", suffix=".yml", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
        os.rename(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the orphan temp file.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _check_binding_fks(binding: Binding, registry) -> None:
    """Fail fast with 422 when a binding references a missing Source.

    Deeper invariants (target uniqueness across bindings) are caught by
    the registry's reload-time `_validate_cross_references` and surface
    through the rollback path."""
    if binding.source not in registry.sources:
        raise HTTPException(
            422,
            f"Binding references unknown source {binding.source!r}; "
            f"known: {sorted(registry.sources)}",
        )


def _safe_reload_with_rollback(
    registry_ref: "IntegrationRegistryRef",
    *,
    rollback_path: Path,
    caller: str,
    action: str,
    rollback_bytes: Optional[bytes] = None,
) -> None:
    """Reload the registry; on validation failure, restore the YAML at
    `rollback_path` to its previous state and raise HTTPException 422 with
    the validation message.

    `rollback_bytes` is the previous file content for an update / delete
    rollback. None means the create case — rollback is simply deleting
    the newly-written file.

    Atomicity contract: from the operator's perspective, a 422 / 400 from
    these endpoints means the YAML directory is unchanged; a 2xx means
    the YAML is updated and the in-memory registry reflects it.
    """
    try:
        registry_ref.reload()
    except IntegrationConfigError as exc:
        _logger.warning(
            "ingest.admin.reload_after_write_failed",
            caller=caller,
            action=action,
            error=str(exc),
        )
        if rollback_bytes is None:
            rollback_path.unlink(missing_ok=True)
        else:
            rollback_path.write_bytes(rollback_bytes)
        try:
            registry_ref.reload()
        except IntegrationConfigError:
            _logger.exception(
                "ingest.admin.rollback_reload_failed",
                caller=caller,
                action=action,
            )
            raise HTTPException(
                500,
                (
                    f"Registry reload failed: {exc}. Rollback also failed — "
                    "the integration directory may be in an inconsistent state."
                ),
            ) from None
        raise HTTPException(422, f"Validation failed: {exc}") from None


def _audit(
    actor_id: str,
    event_type: str,
    target_id: Optional[str],
    details: dict,
) -> None:
    """Write a `security_audit_log` row — matches the admin-surface
    audit pattern."""
    from precis_mcp import db

    db.write_security_audit(
        event_type, actor_id, target_user_id=target_id, details=details,
    )


__all__ = ["build_admin_router"]
