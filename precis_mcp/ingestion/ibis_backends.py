# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Single Ibis backend factory, shared by ingestion and federated reads.

A `Source` is declared once (`instance/integrations/sources/<id>.yml`) and
consumed by two paths — the ingestion executor (operator-authored SQL run as
passthrough) and the federated reader (catalogue-derived Ibis expressions).
Both construct their connection here, so a new warehouse kind is wired in
exactly one place: add a `(ibis-attr, extra, kwargs-resolver, finalize)` entry
to `_BACKENDS`.

Credentials never live in YAML. Each builder reads `<SECRET_REF_UPPER>_*`
env vars (the same convention the secret-manager binding feeds). The kwargs
resolvers are pure — env in, dict out — so they unit-test without a driver.
The `ibis.<backend>.connect(...)` call is the only impure step.

Optional drivers: `postgres` + `duckdb` ship in the default install
(`ibis-framework[duckdb,postgres]`); `mssql` / `snowflake` / `bigquery` /
`databricks` are optional extras (`pip install 'precis-finance-mcp[snowflake]'`) and
raise `IntegrationConfigError` naming the extra when the driver is absent.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from precis_mcp.ingestion.registry import IntegrationConfigError, Source


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _prefix(source: Source) -> str:
    return source.secret_ref.upper()


def _require(source: Source, prefix: str, *suffixes: str) -> dict[str, str]:
    """Read required env vars; raise listing every one that is missing."""
    values = {s: os.environ.get(f"{prefix}_{s}") for s in suffixes}
    missing = [s for s, v in values.items() if not v]
    if missing:
        raise IntegrationConfigError(
            f"Source {source.id!r} (kind={source.kind}) is missing required "
            f"env vars: {[f'{prefix}_{s}' for s in missing]}"
        )
    return values  # type: ignore[return-value]


def _optional(prefix: str, kwargs: dict[str, Any], mapping: tuple[tuple[str, str], ...]) -> None:
    """Copy present optional env vars into kwargs under their kwarg name."""
    for suffix, key in mapping:
        value = os.environ.get(f"{prefix}_{suffix}")
        if value:
            kwargs[key] = value


# ---------------------------------------------------------------------------
# Per-kind kwargs resolvers (pure: env -> dict, no driver import)
# ---------------------------------------------------------------------------


def _kwargs_postgres(source: Source) -> dict[str, Any]:
    p = _prefix(source)
    req = _require(source, p, "HOST", "USER", "DATABASE")
    kwargs: dict[str, Any] = dict(
        host=req["HOST"],
        user=req["USER"],
        database=req["DATABASE"],
        password=os.environ.get(f"{p}_PASSWORD") or "",
        port=int(os.environ.get(f"{p}_PORT") or 5432),
    )
    # TLS is opt-in per source. A warehouse reached over a VPC or the internet
    # carries financial data + DWH credentials, so set `<REF>_SSLMODE=verify-full`
    # (+ `_SSLROOTCERT`) for any remote source — not `require`, which validates
    # no server identity.
    sslmode = os.environ.get(f"{p}_SSLMODE")
    if sslmode:
        kwargs["sslmode"] = sslmode
        sslrootcert = os.environ.get(f"{p}_SSLROOTCERT")
        if sslrootcert:
            kwargs["sslrootcert"] = sslrootcert
    return kwargs


def _kwargs_mssql(source: Source) -> dict[str, Any]:
    p = _prefix(source)
    req = _require(source, p, "HOST", "USER", "DATABASE")
    return dict(
        host=req["HOST"],
        user=req["USER"],
        database=req["DATABASE"],
        password=os.environ.get(f"{p}_PASSWORD") or "",
        port=int(os.environ.get(f"{p}_PORT") or 1433),
        driver=os.environ.get(f"{p}_DRIVER") or "ODBC Driver 18 for SQL Server",
    )


def _kwargs_snowflake(source: Source) -> dict[str, Any]:
    p = _prefix(source)
    req = _require(source, p, "ACCOUNT", "USER", "DATABASE", "WAREHOUSE")
    kwargs: dict[str, Any] = dict(
        account=req["ACCOUNT"],
        user=req["USER"],
        database=req["DATABASE"],
        warehouse=req["WAREHOUSE"],
        password=os.environ.get(f"{p}_PASSWORD") or "",
    )
    _optional(p, kwargs, (("SCHEMA", "schema"), ("ROLE", "role")))
    return kwargs


def _kwargs_bigquery(source: Source) -> dict[str, Any]:
    p = _prefix(source)
    req = _require(source, p, "PROJECT_ID")
    kwargs: dict[str, Any] = dict(project_id=req["PROJECT_ID"])
    _optional(p, kwargs, (("DATASET_ID", "dataset_id"),))
    # The service-account key is a value secret delivered through the platform
    # secret seam: `<REF>_CREDENTIALS_JSON` holds the key JSON itself (or
    # `<REF>_CREDENTIALS_JSON_FILE`, resolved into it by precis_mcp/secrets.py
    # like every other secret). Parsed into a credentials object in
    # `_finalize_bigquery`. Omit it to fall back to Application Default
    # Credentials (GOOGLE_APPLICATION_CREDENTIALS, gcloud ADC, metadata server).
    creds_json = os.environ.get(f"{p}_CREDENTIALS_JSON")
    if creds_json:
        kwargs["credentials_json"] = creds_json
    return kwargs


def _kwargs_databricks(source: Source) -> dict[str, Any]:
    p = _prefix(source)
    req = _require(source, p, "SERVER_HOSTNAME", "HTTP_PATH", "ACCESS_TOKEN")
    kwargs: dict[str, Any] = dict(
        server_hostname=req["SERVER_HOSTNAME"],
        http_path=req["HTTP_PATH"],
        access_token=req["ACCESS_TOKEN"],
    )
    _optional(p, kwargs, (("CATALOG", "catalog"), ("SCHEMA", "schema")))
    return kwargs


def _kwargs_http_upload(source: Source) -> dict[str, Any]:
    # In-memory DuckDB. The binding's `extract.query` references files via
    # `read_csv('${source_path}/<file>.csv')`; the orchestrator substitutes
    # `${source_path}` before execution. No credentials — LocalFs is open.
    del source  # uniform resolver signature; http_upload reads no env
    return {}


# ---------------------------------------------------------------------------
# Driver-side finalizers (run only once the driver is importable)
# ---------------------------------------------------------------------------


def _identity(kwargs: dict[str, Any]) -> dict[str, Any]:
    return kwargs


def _finalize_bigquery(kwargs: dict[str, Any]) -> dict[str, Any]:
    creds_json = kwargs.pop("credentials_json", None)
    if creds_json:
        from google.oauth2 import service_account  # bigquery extra

        kwargs["credentials"] = service_account.Credentials.from_service_account_info(
            json.loads(creds_json)
        )
    return kwargs


# ---------------------------------------------------------------------------
# Dispatch table — the single extension point for a new warehouse kind
# ---------------------------------------------------------------------------

# kind -> (ibis backend attribute, optional-extra name | None, kwargs resolver, finalize)
_BACKENDS: dict[str, tuple[str, str | None, Callable[[Source], dict[str, Any]], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "postgres": ("postgres", None, _kwargs_postgres, _identity),
    "http_upload": ("duckdb", None, _kwargs_http_upload, _identity),
    "mssql": ("mssql", "mssql", _kwargs_mssql, _identity),
    "snowflake": ("snowflake", "snowflake", _kwargs_snowflake, _identity),
    "bigquery": ("bigquery", "bigquery", _kwargs_bigquery, _finalize_bigquery),
    "databricks": ("databricks", "databricks", _kwargs_databricks, _identity),
}


def _unsupported(source: Source) -> IntegrationConfigError:
    return IntegrationConfigError(
        f"Source {source.id!r} declares kind={source.kind!r}, which is not a "
        f"supported Ibis backend. Supported kinds: {', '.join(sorted(_BACKENDS))}. "
        f"Add a branch to precis_mcp/ingestion/ibis_backends.py and document it "
        f"in configuration/ingestion.md when extending."
    )


def resolve_connect_kwargs(source: Source) -> dict[str, Any]:
    """The pure half: env vars -> connect kwargs. Raises on missing vars or
    an unsupported kind. No driver import — safe to unit-test for every kind."""
    spec = _BACKENDS.get(source.kind)
    if spec is None:
        raise _unsupported(source)
    return spec[2](source)


def build_ibis_backend(source: Source) -> Any:
    """Construct an Ibis backend connection for one Source.

    The single connection-construction path for both the ingestion executor
    and the federated reader.
    """
    spec = _BACKENDS.get(source.kind)
    if spec is None:
        raise _unsupported(source)
    attr, extra, resolve, finalize = spec
    kwargs = resolve(source)

    import ibis  # type: ignore[import-not-found]

    try:
        backend = getattr(ibis, attr)
        return backend.connect(**finalize(kwargs))
    except (ImportError, ModuleNotFoundError) as exc:
        if extra:
            raise IntegrationConfigError(
                f"Source {source.id!r} (kind={source.kind}) needs the optional "
                f"{extra!r} driver, which is not installed. Install it with: "
                f"pip install 'precis-finance-mcp[{extra}]'."
            ) from exc
        raise
