# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Wiring — composes the runtime `OrchestratorContext` from real services
and exposes the MCP / FastAPI attachment helpers.

The only module under `precis_mcp/ingestion/` that depends on concrete
infrastructure (Postgres, ClickHouse, Ibis backends, JWT).
Keeping it isolated lets every other module in the package stay
unit-testable without those dependencies.

Also hosts `IntegrationRegistryRef` — the mutable singleton swapped by
the `reload_integrations` MCP tool, analogous to `CatalogueRef` on the
MCP server.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from precis_mcp.ingestion import ibis_backends
from precis_mcp.ingestion.load_history import PostgresLoadHistoryWriter
from precis_mcp.ingestion.orchestrator import OrchestratorContext
from precis_mcp.ingestion.registry import (
    IntegrationConfigError,
    IntegrationRegistry,
    Source,
)
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.wiring")


# ---------------------------------------------------------------------------
# Env-var contracts
# ---------------------------------------------------------------------------


INTEGRATIONS_ROOT_ENV = "PRECIS_INTEGRATIONS_ROOT"
UPLOAD_DIR_ENV = "PRECIS_INGEST_UPLOAD_DIR"


def _default_integrations_root() -> Path:
    """Resolve `instance/integrations/` at the repo root.

    Override with `PRECIS_INTEGRATIONS_ROOT=/some/path`. The path
    structure expected is `<root>/sources/*.yml` + `<root>/bindings/*.yml`.
    """
    val = os.environ.get(INTEGRATIONS_ROOT_ENV)
    if val:
        return Path(val)
    # precis_mcp/ingestion/wiring.py → repo root is parent.parent.parent
    return (
        Path(__file__).resolve().parent.parent.parent
        / "instance"
        / "integrations"
    )


def _default_upload_dir() -> Path:
    val = os.environ.get(UPLOAD_DIR_ENV)
    if val:
        return Path(val)
    return Path("/var/lib/precis/ingest/uploads")


# ---------------------------------------------------------------------------
# IntegrationRegistryRef — mutable singleton for hot reload
# ---------------------------------------------------------------------------


class IntegrationRegistryRef:
    """Mutable reference to the active `IntegrationRegistry`.

    Parallel to `CatalogueRef` on the MCP server. Atomic swap on
    reload — a failed reload leaves `current` pointing at the previous
    valid registry.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.current: IntegrationRegistry = IntegrationRegistry.load(self.root)

    def reload(self) -> str:
        """Re-read the directory; swap atomically on success.

        Returns a human-readable summary line. Raises
        IntegrationConfigError on validation failure (the previous
        `current` stays in place).
        """
        try:
            new = IntegrationRegistry.load(self.root)
        except IntegrationConfigError:
            _logger.warning(
                "ingestion.registry.reload_failed",
                root=str(self.root),
            )
            raise
        self.current = new
        summary = (
            f"Reloaded {self.root} — "
            f"sources={len(new.sources)} bindings={len(new.bindings)}"
        )
        _logger.info("ingestion.registry.reload_succeeded", summary=summary)
        return summary


# ---------------------------------------------------------------------------
# Client constructors
# ---------------------------------------------------------------------------


def _default_ch_client() -> Any:
    """Construct a clickhouse-connect client from env vars."""
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=os.getenv("CHHOST", "localhost"),
        port=int(os.getenv("CHPORT", "8123")),
        username=os.getenv("CHUSER", "default"),
        password=os.getenv("CHPASSWORD", ""),
        database=os.getenv("CHDATABASE", "default"),
    )


def _pg_advisory_lock_factory(key: str, *, timeout: int, blocking_timeout: int):
    """Default ingestion lock — Postgres session-level advisory lock.

    ``timeout`` (the old Redis TTL) is accepted for seam compatibility and
    ignored: the lock is held until release or connection death, which cannot
    expire under a still-running load the way a TTL can.
    """
    del timeout
    from precis_mcp.ingestion.pg_lock import PgAdvisoryLock

    return PgAdvisoryLock(key, blocking_timeout=blocking_timeout)


# ---------------------------------------------------------------------------
# Ibis backend resolution — per Source, cached per-registry
# ---------------------------------------------------------------------------


def _make_ibis_resolver(registry: IntegrationRegistry) -> Callable[[str], Any]:
    """Return a `resolve(source_id) -> backend` callable that caches
    constructed backends per source id. Cache cleared by recreating the
    resolver after a registry reload.
    """
    cache: dict[str, Any] = {}

    def resolve(source_id: str) -> Any:
        if source_id in cache:
            return cache[source_id]
        source = registry.get_source(source_id)
        backend = ibis_backends.build_ibis_backend(source)
        cache[source_id] = backend
        return backend

    return resolve


def _resolve_source_path(source: Source) -> Optional[str]:
    """Resolve a file-drop source to an absolute directory path on the
    host filesystem, or None for non-file sources.

    Joins `PRECIS_INGEST_UPLOAD_DIR` with `source.backend.prefix` so the
    binding's extract query can reference files as
    `'${source_path}/<file>.csv'` regardless of where the deployment
    mounts its upload root.

    Only `kind: http_upload` is wired today. `kind: s3` / `kind: sftp`
    will land here once DuckDB credential setup is wired.
    """
    if source.kind != "http_upload":
        return None
    upload_root = os.environ.get(UPLOAD_DIR_ENV)
    if not upload_root:
        return None
    prefix = (source.backend or {}).get("prefix", "")
    # Trim leading slash on prefix so os.path.join treats it as
    # relative; trim trailing slash so the resolved path has no double
    # separator.
    prefix = prefix.lstrip("/").rstrip("/")
    if prefix:
        return os.path.join(upload_root, prefix)
    return upload_root


def _make_source_path_resolver(
    registry: IntegrationRegistry,
) -> Callable[[str], Optional[str]]:
    """Return `resolve(source_id) -> Optional[str]` for file-drop
    source paths. Returns None for non-file sources or when no upload
    root is configured."""

    def resolve(source_id: str) -> Optional[str]:
        try:
            source = registry.get_source(source_id)
        except Exception:
            return None
        return _resolve_source_path(source)

    return resolve


# ---------------------------------------------------------------------------
# build_default_context — wire the production OrchestratorContext
# ---------------------------------------------------------------------------


def build_default_context(
    registry: IntegrationRegistry,
    *,
    ch_client: Optional[Any] = None,
    lock_factory: Optional[Callable[..., Any]] = None,
    ibis_backend_for_source: Optional[Callable[[str], Any]] = None,
    source_path_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> OrchestratorContext:
    """Compose the production `OrchestratorContext` from real services.

    Callers can override any dependency (used by integration tests that
    wire a real Postgres + dev ClickHouse). Defaults read from env vars
    matching the rest of the codebase (`CH*` for ClickHouse, `PG*` for the
    platform DB that also backs the advisory lock,
    `<SECRET_REF_UPPER>_*` for Ibis backend credentials).
    """
    ch = ch_client or _default_ch_client()
    ibis_resolver = ibis_backend_for_source or _make_ibis_resolver(registry)
    path_resolver = source_path_resolver or _make_source_path_resolver(registry)

    return OrchestratorContext(
        registry=registry,
        load_history=PostgresLoadHistoryWriter(),
        lock_factory=lock_factory or _pg_advisory_lock_factory,
        ch_client=ch,
        ibis_backend_for_source=ibis_resolver,
        source_path_resolver=path_resolver,
    )


# ---------------------------------------------------------------------------
# Deployment-time attachment — called from the server entrypoint at startup
# ---------------------------------------------------------------------------


def attach_ingestion_to_mcp(
    mcp,
    *,
    integrations_root: Optional[Path] = None,
) -> IntegrationRegistryRef:
    """Construct the `IntegrationRegistryRef` singleton and register
    `reload_integrations` on the MCP server.

    Called once from the MCP server entrypoint after the `FastMCP`
    instance is created. Returns the ref so other surfaces (admin
    routes, agent tools) can read from it.
    """
    from precis_mcp.tools.ingestion_tools import register_ingestion_tools

    ref = IntegrationRegistryRef(
        integrations_root or _default_integrations_root()
    )
    register_ingestion_tools(mcp, ref)
    _logger.info(
        "ingestion.wiring.mcp_attached",
        sources=len(ref.current.sources),
        bindings=len(ref.current.bindings),
    )
    return ref


def attach_ingestion_to_app(
    app,
    *,
    upload_dir: Optional[Path] = None,
    token_verifier: Optional[Callable] = None,
) -> bool:
    """Mount the upload router on a FastAPI app. Returns True when
    mounted.

    Skipped when neither `upload_dir` is passed nor
    `PRECIS_INGEST_UPLOAD_DIR` is set — keeps dev imports of the API
    app from requiring privileged filesystem paths.
    """
    from precis_mcp.ingestion.object_store import LocalFsObjectStore
    from precis_mcp.ingestion.upload_routes import build_upload_router

    target_dir = upload_dir
    if target_dir is None:
        env_dir = os.environ.get(UPLOAD_DIR_ENV)
        if not env_dir:
            _logger.info(
                "ingestion.wiring.app_attach_skipped",
                reason="no upload dir configured",
                env_var=UPLOAD_DIR_ENV,
            )
            return False
        target_dir = Path(env_dir)

    try:
        store = LocalFsObjectStore(target_dir)
    except OSError as exc:
        _logger.warning(
            "ingestion.wiring.upload_store_init_failed",
            upload_dir=str(target_dir),
            error=str(exc),
        )
        return False

    verifier = token_verifier or _default_token_verifier
    router = build_upload_router(token_verifier=verifier, upload_store=store)
    app.include_router(router)
    _logger.info(
        "ingestion.wiring.app_attached",
        upload_dir=str(target_dir),
    )
    return True


def _default_token_verifier(token: str):
    """JWT-backed verifier — extracts `binding_ids` + identity + roles
    from claims. Expected claims:

      - `binding_ids` (required): list of binding ids the caller is
        allowed to target. Singular `binding_id` accepted for
        back-compat.
      - `user_id` / `sub`: caller identity for audit.
      - `roles` (optional): list of role names. Push endpoint gates on
        `admin` / `plan_manager`; upload endpoint doesn't read roles.
    """
    from precis_mcp.ingestion.binding_auth import verify_binding_token
    from precis_mcp.ingestion.upload_routes import BindingScopedToken

    claims = verify_binding_token(token)
    binding_ids_claim = claims.get("binding_ids")
    if binding_ids_claim is None:
        singular = claims.get("binding_id")
        if singular:
            binding_ids_claim = [singular]
    if not binding_ids_claim:
        raise ValueError(
            "token missing required 'binding_ids' claim (or 'binding_id')"
        )
    if not isinstance(binding_ids_claim, (list, tuple)):
        raise ValueError("'binding_ids' claim must be a list")
    caller = claims.get("user_id") or claims.get("sub") or "unknown"

    roles_claim = claims.get("roles")
    if roles_claim is None:
        singular_role = claims.get("role")
        roles_claim = [singular_role] if singular_role else []
    if not isinstance(roles_claim, (list, tuple)):
        raise ValueError("'roles' claim must be a list")

    return BindingScopedToken(
        binding_ids=tuple(str(b) for b in binding_ids_claim),
        caller=caller,
        roles=tuple(str(r) for r in roles_claim),
    )


# ---------------------------------------------------------------------------
# Object stores — kept for file-drop daemons. The orchestrator does not
# use them (Ibis owns warehouse credentials); file-drop ingestion is a
# separate code path.
# ---------------------------------------------------------------------------


def build_object_stores_from_env() -> dict[str, Any]:
    """Construct the configured object stores keyed by `source.kind`.

    A store is included only when its primary identity env var is set:

      - `PRECIS_S3_BUCKET` → s3
      - `PRECIS_SFTP_HOST` → sftp
      - `PRECIS_INGEST_UPLOAD_DIR` → http_upload (LocalFs)
    """
    from precis_mcp.ingestion.object_store import LocalFsObjectStore

    out: dict[str, Any] = {}

    if os.getenv("PRECIS_S3_BUCKET"):
        from precis_mcp.ingestion.object_store import S3ObjectStore

        out["s3"] = S3ObjectStore(
            bucket=os.environ["PRECIS_S3_BUCKET"],
            prefix=os.getenv("PRECIS_S3_PREFIX", ""),
            region=os.getenv("PRECIS_S3_REGION"),
            endpoint_url=os.getenv("PRECIS_S3_ENDPOINT_URL"),
            access_key_id=os.getenv("PRECIS_S3_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("PRECIS_S3_SECRET_ACCESS_KEY"),
        )
        _logger.info("ingestion.wiring.store_built", kind="s3")

    if os.getenv("PRECIS_SFTP_HOST"):
        from precis_mcp.ingestion.object_store import SftpObjectStore

        out["sftp"] = SftpObjectStore(
            host=os.environ["PRECIS_SFTP_HOST"],
            username=os.environ.get("PRECIS_SFTP_USER", ""),
            password=os.getenv("PRECIS_SFTP_PASSWORD"),
            port=int(os.getenv("PRECIS_SFTP_PORT", "22")),
            private_key_path=os.getenv("PRECIS_SFTP_KEY_PATH"),
            prefix=os.getenv("PRECIS_SFTP_PREFIX", ""),
            known_hosts_path=os.getenv("PRECIS_SFTP_KNOWN_HOSTS"),
            host_key=os.getenv("PRECIS_SFTP_HOST_KEY"),
        )
        _logger.info("ingestion.wiring.store_built", kind="sftp")

    if os.getenv(UPLOAD_DIR_ENV):
        out["http_upload"] = LocalFsObjectStore(os.environ[UPLOAD_DIR_ENV])
        _logger.info("ingestion.wiring.store_built", kind="http_upload")

    return out


def build_store_factory(stores: dict[str, Any]) -> Callable:
    """Return a `store_factory(source)` callable that looks up by
    `source.kind`. Raises KeyError when a source's kind has no
    configured store."""

    def factory(source):
        return stores[source.kind]

    return factory


# ---------------------------------------------------------------------------
# Full-stack ingestion context — env-driven bootstrap
# ---------------------------------------------------------------------------


def build_default_ingestion_context_from_env(
    *,
    integrations_root: Optional[Path] = None,
) -> Optional[OrchestratorContext]:
    """Build the complete production `OrchestratorContext` from env
    vars.

    Returns None when prerequisites (CH connectivity, registry
    directory) aren't satisfied — callers that opt-in via env vars
    (the push-endpoint attachment) treat None as "skip wiring, log
    only."
    """
    root = integrations_root or _default_integrations_root()
    try:
        registry = IntegrationRegistry.load(root)
    except Exception as exc:
        _logger.warning(
            "ingestion.wiring.context_build_failed",
            stage="registry_load",
            error=str(exc),
        )
        return None

    try:
        ch_client = _default_ch_client()
    except Exception as exc:
        _logger.warning(
            "ingestion.wiring.context_build_failed",
            stage="ch_client",
            error=str(exc),
        )
        return None

    return build_default_context(registry, ch_client=ch_client)


# ---------------------------------------------------------------------------
# Convenience for routes that mount admin / run endpoints
# ---------------------------------------------------------------------------


def attach_admin_routes_to_app(
    app,
    *,
    registry_ref: Optional["IntegrationRegistryRef"] = None,
    orchestrator_ctx: Optional[OrchestratorContext] = None,
) -> bool:
    """Mount the `/api/admin/*` ingestion routes on a FastAPI app.

    `registry_ref` is the host-side `IntegrationRegistryRef` — same one
    `attach_ingestion_to_app` constructed. `orchestrator_ctx` makes the
    trigger endpoint functional; when None it returns 503.
    """
    from precis_mcp.ingestion.admin_routes import build_admin_router

    if registry_ref is None:
        _logger.info(
            "ingestion.wiring.admin_routes_skipped",
            reason="no IntegrationRegistryRef provided",
        )
        return False

    router = build_admin_router(
        registry_ref=registry_ref,
        orchestrator_ctx=orchestrator_ctx,
    )
    app.include_router(router)
    _logger.info(
        "ingestion.wiring.admin_routes_attached",
        trigger_enabled=orchestrator_ctx is not None,
    )
    return True


def attach_run_endpoint_to_app(
    app,
    *,
    ctx: Optional[OrchestratorContext] = None,
    token_verifier: Optional[Callable] = None,
) -> bool:
    """Mount `POST /api/ingest/run` on a FastAPI app. Returns True when
    mounted. `ctx` required — None → log+skip."""
    from precis_mcp.ingestion.run_routes import build_run_router

    if ctx is None:
        _logger.info(
            "ingestion.wiring.run_endpoint_skipped",
            reason="no orchestrator context provided",
        )
        return False

    verifier = token_verifier or _default_token_verifier
    router = build_run_router(ctx=ctx, token_verifier=verifier)
    app.include_router(router)
    _logger.info("ingestion.wiring.run_endpoint_attached")
    return True
