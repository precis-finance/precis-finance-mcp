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

Backend construction
--------------------
Connections are built by the shared `precis_mcp.ingestion.ibis_backends`
factory — the single place a warehouse kind is wired, consumed by both this
federated path and the ingestion executor. Add a new kind there, not here.
"""

from __future__ import annotations

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
# `precis_mcp/engine/__init__.py` and `precis_mcp/tools/read_tools.py`.
# ---------------------------------------------------------------------------


_DEFAULT = IbisRegistry()


def get_ibis_backend(backend: str) -> Any:
    return _DEFAULT.get_ibis_backend(backend)


def get_ibis_backends(required) -> dict[str, Any]:
    return _DEFAULT.get_ibis_backends(required)


def set_integration_registry(registry_or_ref) -> None:
    """Wire the default singleton's IntegrationRegistry source.

    Called from the server entrypoints at startup
    so the federated path resolves Sources from the live registry. A reload
    of the integration catalogue clears the connection cache.
    """
    _DEFAULT.set_integration_registry(registry_or_ref)


# ---------------------------------------------------------------------------
# Connection construction — delegated to the shared factory
# ---------------------------------------------------------------------------


def _connect_for_source(source) -> Any:
    """Build the Ibis connection for a federated Source via the shared
    `precis_mcp.ingestion.ibis_backends` factory — the same one the ingestion
    executor uses. The factory raises `IntegrationConfigError`; translate it to
    `IbisRegistryError` to preserve this module's contract."""
    from precis_mcp.ingestion import ibis_backends
    from precis_mcp.ingestion.registry import IntegrationConfigError

    try:
        return ibis_backends.build_ibis_backend(source)
    except IntegrationConfigError as exc:
        raise IbisRegistryError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Lazy default IntegrationRegistry — used when set_integration_registry has
# not been called (e.g. ad-hoc tooling, tests that exercise the registry
# without wiring through the server entrypoints).
# ---------------------------------------------------------------------------


def _build_default_integration_registry():
    from precis_mcp.ingestion.registry import IntegrationRegistry
    from precis_mcp.ingestion.wiring import _default_integrations_root  # noqa: PLC2701

    return IntegrationRegistry.load(_default_integrations_root())
