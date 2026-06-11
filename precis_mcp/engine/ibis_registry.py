# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Ibis backend registry for federated-read domains.

Resolves a catalogue domain's `backend: <id>` value to an Ibis connection,
consulting the **same `Source` objects** the ingestion subsystem uses
(`instance/integrations/sources/<id>.yml`). One Source declaration serves
both consumers — federated queries and warehouse ingestion — so a customer
warehouse is registered exactly once and both paths see consistent
credentials.

Connections are cached per source id; the cache clears when
`set_integration_registry()` is called (typically on hot-reload of the
integration catalogue).

Credentials resolution
----------------------
Each Source declares a `secret_ref`. The Ibis registry reads env vars
prefixed by `<SECRET_REF_UPPER>_*` — the same convention the Pattern A
named-collection wiring and the warehouse drivers use. Example:

    Source.secret_ref = "customer_pg"
        → CUSTOMER_PG_HOST, CUSTOMER_PG_USER, CUSTOMER_PG_PASSWORD,
          CUSTOMER_PG_DATABASE, CUSTOMER_PG_PORT (optional, default 5432).

Phase-one scope
---------------
- Postgres is the only Ibis backend wired today.
- Other Ibis backends (Snowflake, BigQuery, MySQL, …) land when federated
  reads needs them; the dispatch in `_connect_for_source` is the extension
  point.
"""

from __future__ import annotations

import os
from typing import Any, Optional


class IbisRegistryError(Exception):
    """Raised when a configured Ibis backend cannot be created."""


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------


class IbisRegistry:
    """Resolves catalogue `backend: <id>` references to Ibis connections.

    Holds an `IntegrationRegistry` or `IntegrationRegistryRef`; defers actual
    creation of the integration registry until first use when not supplied.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._registry: Optional[Any] = None  # IntegrationRegistry | Ref

    def set_integration_registry(self, registry_or_ref: Any) -> None:
        """Wire the source-of-truth registry. Accepts either an
        `IntegrationRegistry` directly or an `IntegrationRegistryRef`. Clears
        the connection cache so a reload rebuilds connections."""
        self._registry = registry_or_ref
        self._cache.clear()

    def get_ibis_backend(self, source_id: str) -> Any:
        if source_id in self._cache:
            return self._cache[source_id]
        source = self._resolve_source(source_id)
        conn = _connect_for_source(source)
        self._cache[source_id] = conn
        return conn

    def get_ibis_backends(self, source_ids) -> dict[str, Any]:
        return {sid: self.get_ibis_backend(sid) for sid in source_ids}

    # -- Internals --------------------------------------------------------

    def _resolve_source(self, source_id: str):
        from precis_mcp.ingestion.registry import IntegrationConfigError

        registry = self._current_integration_registry()
        try:
            return registry.get_source(source_id)
        except IntegrationConfigError as exc:
            raise IbisRegistryError(
                f"No Source declared for federated backend {source_id!r}. "
                f"Add instance/integrations/sources/{source_id}.yml — the "
                f"federated catalogue domain references it by id."
            ) from exc

    def _current_integration_registry(self):
        if self._registry is None:
            self._registry = _build_default_integration_registry()
        # `IntegrationRegistryRef` exposes the active registry on `.current`;
        # a plain `IntegrationRegistry` is the registry itself.
        return getattr(self._registry, "current", self._registry)


# ---------------------------------------------------------------------------
# Module-level singleton — preserves the existing function-level API
# `get_ibis_backend(name)` / `get_ibis_backends(set)` for callers in
# `precis_mcp/engine/__init__.py` and `precis_mcp/tools/read_tools.py` so
# this refactor is a runtime-internal change.
# ---------------------------------------------------------------------------


_DEFAULT = IbisRegistry()


def get_ibis_backend(backend: str) -> Any:
    return _DEFAULT.get_ibis_backend(backend)


def get_ibis_backends(required) -> dict[str, Any]:
    return _DEFAULT.get_ibis_backends(required)


def set_integration_registry(registry_or_ref) -> None:
    """Wire the default singleton's IntegrationRegistry source.

    Called from `precis/server.py` and `precis/agui.py` at startup
    so the federated path resolves Sources from the live registry. A reload
    of the integration catalogue clears the connection cache.
    """
    _DEFAULT.set_integration_registry(registry_or_ref)


# ---------------------------------------------------------------------------
# Per-kind connection construction — extension point for new Ibis backends
# ---------------------------------------------------------------------------


def _connect_for_source(source) -> Any:
    if source.kind == "postgres":
        return _connect_postgres(source)
    raise IbisRegistryError(
        f"Source {source.id!r} declares kind={source.kind!r} which is not "
        f"yet supported as an Ibis federated backend. Currently supported "
        f"kinds: postgres. Add a new branch to ibis_registry._connect_for_source "
        f"and document in docs/architecture/03-integration.md when extending."
    )


def _connect_postgres(source) -> Any:
    try:
        import ibis  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — deploy-time
        raise IbisRegistryError(
            "ibis-framework[postgres] is required for federated Postgres domains"
        ) from exc

    prefix = source.secret_ref.upper()
    env = os.environ
    host = env.get(f"{prefix}_HOST")
    database = env.get(f"{prefix}_DATABASE")
    user = env.get(f"{prefix}_USER")
    password = env.get(f"{prefix}_PASSWORD") or ""
    port = int(env.get(f"{prefix}_PORT") or 5432)
    missing = [
        suffix for suffix, value in (("HOST", host), ("USER", user), ("DATABASE", database))
        if not value
    ]
    if missing:
        raise IbisRegistryError(
            f"Source {source.id!r} (kind=postgres) is missing required env "
            f"vars: {[f'{prefix}_{s}' for s in missing]}"
        )

    connect_kwargs: dict[str, Any] = dict(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )
    # TLS is opt-in per source. A federated read against a remote customer
    # warehouse carries financial data over a VPC / the internet, so set
    # `<SECRET_REF>_SSLMODE=verify-full` (+ `_SSLROOTCERT`) for any remote
    # source — not `require`, which validates no server identity.
    sslmode = env.get(f"{prefix}_SSLMODE")
    if sslmode:
        connect_kwargs["sslmode"] = sslmode
        sslrootcert = env.get(f"{prefix}_SSLROOTCERT")
        if sslrootcert:
            connect_kwargs["sslrootcert"] = sslrootcert
    return ibis.postgres.connect(**connect_kwargs)


# ---------------------------------------------------------------------------
# Lazy default IntegrationRegistry — used when set_integration_registry has
# not been called (e.g. ad-hoc tooling, tests that exercise the registry
# without wiring through server.py / agui.py).
# ---------------------------------------------------------------------------


def _build_default_integration_registry():
    from precis_mcp.ingestion.registry import IntegrationRegistry
    from precis_mcp.ingestion.wiring import _default_integrations_root  # noqa: PLC2701

    return IntegrationRegistry.load(_default_integrations_root())
