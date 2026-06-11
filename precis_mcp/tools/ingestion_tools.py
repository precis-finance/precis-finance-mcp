# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""MCP tools for the ingestion subsystem.

Read tools (`list_load_history`, `get_load_status`, `list_bindings`,
`get_binding`) answer the analyst's "did April land?" and "what sources do
we have?" questions without any orchestrator trigger.

`reload_integrations` is the one administrative exception — analogous to
the existing `reload_catalogue` for the metric catalogue.

Write operations (trigger a load, override a lock, register sources or
bindings) live in the admin UI behind explicit operator confirmation; they
are never invoked by an LLM tool call (see `docs/architecture/03-integration.md`
"Current state" — Admin REST surface).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from precis_mcp.ingestion.wiring import IntegrationRegistryRef


def register_ingestion_tools(
    mcp: "FastMCP",
    registry_ref: "IntegrationRegistryRef",
) -> None:
    """Register ingestion-related MCP tools on the server instance."""

    @mcp.tool()
    def reload_integrations() -> str:
        """Reload the integration registry from disk without restarting.

        Re-reads `instance/integrations/{sources,bindings}/*.yml`,
        validates everything, and atomically swaps the active registry on
        success. A failed validation leaves the previous registry untouched.

        Also re-binds the `IbisRegistry` to the (same) `IntegrationRegistryRef`,
        which clears its connection cache so federated reads pick up changed
        Source credentials and `kind` on the next query — otherwise stale
        Ibis connections would survive the reload.

        Use this after editing any YAML in `instance/integrations/` to make
        changes live immediately.
        """
        summary = registry_ref.reload()
        from precis_mcp.engine.ibis_registry import set_integration_registry

        set_integration_registry(registry_ref)
        return summary

    # -- Agent read tools --------------------------------------------------

    @mcp.tool()
    def list_load_history(
        binding_id: str | None = None,
        dataset_id: str | None = None,
        period: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List recent ingestion attempts from `load_history`.

        All filters are optional and combine with AND. Returns up to `limit`
        rows (hard-capped at 200) ordered most-recent-first.

        Use this to answer "did April GL from NetSuite land?", "what failed
        in the last hour?", or "show me every load that's still running."

        Args:
            binding_id: e.g. 'netsuite_prod__gl' — restrict to one binding.
            dataset_id: e.g. 'gl' — restrict to one dataset across sources.
            period: 'YYYYMM' — restrict to one accounting period.
            status: one of 'running' / 'success' / 'failed_extract' /
                'failed_recon' / 'failed_swap' / 'failed_dbt' /
                'failed_validation' / 'failed_other'.
            limit: row cap (default 50, max 200).
        """
        from precis_mcp.ingestion.load_history import query_load_history

        rows = query_load_history(
            binding_id=binding_id,
            dataset_id=dataset_id,
            period=period,
            status=status,
            limit=limit,
        )
        return {"rows": _serialise_load_rows(rows), "count": len(rows)}

    @mcp.tool()
    def get_load_status(load_id: str) -> dict:
        """Fetch one `load_history` row by `load_id` — full detail including
        timestamps, status, and any error message.

        Use this when `list_load_history` surfaces a problematic load and
        you need the dbt test failure detail or the exception text. Per-test
        results live in dbt's run_results.json on the orchestrator host, not
        on the load_history row.
        """
        from precis_mcp.ingestion.load_history import get_load_history_row

        row = get_load_history_row(load_id)
        if row is None:
            return {"found": False, "load_id": load_id}
        return {"found": True, "row": _serialise_load_rows([row])[0]}

    @mcp.tool()
    def list_bindings(
        source_id: str | None = None,
        target: str | None = None,
    ) -> dict:
        """List active bindings with their schedule mode and configuration.

        Optional filters (combined with AND): `source_id` to scope to one
        source, `target` to scope to one live table (e.g. 'live.fact_gl').

        Use this to answer "what bindings target NetSuite?" or "what's the
        schedule for the bindings that fill live.fact_gl?"
        """
        registry = registry_ref.current
        bindings = list(registry.bindings.values())
        if source_id is not None:
            bindings = [b for b in bindings if b.source == source_id]
        if target is not None:
            bindings = [b for b in bindings if b.target == target]
        return {
            "bindings": [_binding_summary(b) for b in bindings],
            "count": len(bindings),
        }

    @mcp.tool()
    def get_binding(binding_id: str) -> dict:
        """Fetch one binding's full configuration.

        Returns the binding's source, dataset, schedule (mode + cron
        expression or watch config), column_map, scenario handling, and
        extract parameters.
        """
        from precis_mcp.ingestion.registry import IntegrationConfigError

        try:
            binding = registry_ref.current.get_binding(binding_id)
        except IntegrationConfigError:
            return {"found": False, "binding_id": binding_id}
        return {"found": True, "binding": binding.model_dump()}


# ---------------------------------------------------------------------------
# Serialisation helpers — agent surfaces want JSON-safe dicts
# ---------------------------------------------------------------------------


def _serialise_load_rows(rows: list[dict]) -> list[dict]:
    """Convert datetime / Decimal columns to ISO strings + float for JSON."""
    from datetime import date, datetime
    from decimal import Decimal

    def _coerce(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    return [{k: _coerce(v) for k, v in row.items()} for row in rows]


def _binding_summary(binding) -> dict:
    """Compact summary suitable for `list_bindings` — full config available
    via `get_binding`. Trims to the operationally-meaningful fields."""
    schedule = binding.schedule
    return {
        "id": binding.id,
        "source": binding.source,
        "target": binding.target,
        "kind": binding.kind,
        "schedule": {
            "mode": schedule.mode,
            "expression": schedule.expression,
            "timezone": schedule.timezone,
            "period_selection": {
                "strategy": schedule.period_selection.strategy,
                "lookback_periods": schedule.period_selection.lookback_periods,
            },
        },
        "scenario": binding.scenario,
    }
