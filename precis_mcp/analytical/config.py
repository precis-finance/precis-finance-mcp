# SPDX-License-Identifier: Elastic-2.0
"""Validated non-secret settings and redacted, env-delivered OAuth material."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import re
from typing import Mapping
from urllib.parse import urlsplit

from .contracts import ConfigurationError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def quote_identifier(value: str) -> str:
    """Quote one namespace component; identifiers are never value parameters."""
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError("Invalid analytical object identifier.")
    return f"`{value}`"


@dataclass(frozen=True)
class DatabricksConfig:
    host: str
    http_path: str
    auth_type: str = "oauth-m2m"
    client_id: str | None = field(default=None, repr=False)
    client_secret: str | None = field(default=None, repr=False)
    profile: str | None = None
    socket_timeout: float = 30
    statement_timeout: int = 60
    catalog: str | None = None
    semantic_schema: str = "semantic"
    planning_schema: str = "precis_planning"
    actuals_schema: str = "actuals"

    def __post_init__(self):
        try:
            url = urlsplit(self.host)
            valid = (url.scheme == "https" and url.hostname and not url.username
                     and not url.password and url.port in (None, 443)
                     and url.path in ("", "/") and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError("DATABRICKS_HOST must be an HTTPS workspace origin.")
        if not re.fullmatch(r"/sql/1\.0/warehouses/[A-Za-z0-9_-]+", self.http_path):
            raise ConfigurationError("DATABRICKS_HTTP_PATH must identify a SQL warehouse.")
        if self.auth_type not in {"oauth-m2m", "databricks-cli"}:
            raise ConfigurationError("Use oauth-m2m, or explicitly select databricks-cli for development.")
        if self.auth_type == "oauth-m2m":
            if not self.client_id or not self.client_secret:
                raise ConfigurationError("DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET are required for oauth-m2m.")
            if self.profile:
                raise ConfigurationError("DATABRICKS_CONFIG_PROFILE is only supported for explicit developer CLI authentication.")
        if not math.isfinite(self.socket_timeout) or not 0 < self.socket_timeout <= 300:
            raise ConfigurationError("Databricks socket timeout must be between 0 and 300 seconds.")
        if type(self.statement_timeout) is not int or not 1 <= self.statement_timeout <= 172800:
            raise ConfigurationError("Databricks statement timeout must be an integer from 1 to 172800 seconds.")
        for identifier in (self.catalog, self.semantic_schema, self.planning_schema, self.actuals_schema):
            if identifier is not None:
                quote_identifier(identifier)

    @property
    def server_hostname(self) -> str:
        return urlsplit(self.host).hostname or ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DatabricksConfig:
        """Read an already-resolved environment; never load dotenv/profiles here."""
        values = os.environ if env is None else env
        try:
            return cls(
                host=values.get("DATABRICKS_HOST", ""),
                http_path=values.get("DATABRICKS_HTTP_PATH", ""),
                auth_type=values.get("DATABRICKS_AUTH_TYPE") or "oauth-m2m",
                client_id=values.get("DATABRICKS_CLIENT_ID"),
                client_secret=values.get("DATABRICKS_CLIENT_SECRET"),
                profile=values.get("DATABRICKS_CONFIG_PROFILE") or None,
                socket_timeout=float(values.get("PRECIS_DATABRICKS_SOCKET_TIMEOUT") or "30"),
                statement_timeout=int(values.get("PRECIS_DATABRICKS_STATEMENT_TIMEOUT") or "60"),
                catalog=values.get("DATABRICKS_CATALOG") or None,
                semantic_schema=values.get("PRECIS_DATABRICKS_SEMANTIC_SCHEMA") or "semantic",
                planning_schema=values.get("PRECIS_DATABRICKS_PLANNING_SCHEMA") or "precis_planning",
                actuals_schema=values.get("PRECIS_DATABRICKS_ACTUALS_SCHEMA") or "actuals",
            )
        except ValueError:
            raise ConfigurationError("Invalid Databricks timeout configuration.") from None
