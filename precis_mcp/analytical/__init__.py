# SPDX-License-Identifier: Elastic-2.0
"""Selected analytical clients shared by the engine and application reads."""
from __future__ import annotations

import os
import atexit

from .config import DatabricksConfig
from .contracts import AnalyticalClient, ConfigurationError, QueryResult
from .resources import PooledQueries, close_analytical_resources, get_pool

atexit.register(close_analytical_resources)


def selected_backend(backend: str | None = None) -> str:
    selected = backend or os.getenv("PRECIS_ANALYTICAL_BACKEND", "clickhouse")
    if selected not in {"clickhouse", "databricks"}:
        raise ConfigurationError("PRECIS_ANALYTICAL_BACKEND must be clickhouse or databricks.")
    return selected


def get_analytical_client(backend: str | None = None, *,
                          databricks_config: DatabricksConfig | None = None) -> AnalyticalClient:
    selected = selected_backend(backend)
    if selected == "clickhouse":
        if databricks_config is not None:
            raise ConfigurationError("Databricks settings were supplied for a ClickHouse client.")
        from .clickhouse import ClickHouseClient
        from precis_mcp.db import clickhouse_connection_options
        options = clickhouse_connection_options()
        pool = get_pool("clickhouse", tuple(sorted(options.items())),
                        lambda: ClickHouseClient(connection_options=options))
        return ClickHouseClient(execution=PooledQueries(pool))
    # Activate the same once-at-startup *_FILE convention used by all other
    # platform credential consumers. No new secret source or dotenv loader.
    from precis_mcp import secrets as _secrets  # noqa: F401
    from .databricks import DatabricksClient, credential_provider
    config = databricks_config or DatabricksConfig.from_env()
    provider = credential_provider(config)
    pool = get_pool("databricks", config,
                    lambda: DatabricksClient(config, credentials_provider=provider))
    return DatabricksClient(config, execution=PooledQueries(pool))


__all__ = ["AnalyticalClient", "DatabricksConfig", "QueryResult", "get_analytical_client",
           "selected_backend", "close_analytical_resources"]
