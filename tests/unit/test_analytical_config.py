# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure validation; no driver imports, authentication or secret-file I/O."""
import pytest

from precis_mcp.analytical.config import DatabricksConfig, quote_identifier
from precis_mcp.analytical.contracts import ConfigurationError


def _env(**overrides):
    return {"DATABRICKS_HOST": "https://workspace.example", "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/warehouse1",
            "DATABRICKS_CLIENT_ID": "principal", "DATABRICKS_CLIENT_SECRET": "never-show-secret", **overrides}


def test_production_config_requires_explicit_env_credentials_and_redacts_them():
    config = DatabricksConfig.from_env(_env())
    assert config.auth_type == "oauth-m2m"
    assert config.server_hostname == "workspace.example"
    assert "never-show-secret" not in repr(config)
    assert "principal" not in repr(config)
    with pytest.raises(ConfigurationError, match="CLIENT_SECRET"):
        DatabricksConfig.from_env(_env(DATABRICKS_CLIENT_SECRET=""))


@pytest.mark.parametrize("host", ["http://workspace.example", "https://user:secret@workspace.example",
                                 "https://workspace.example/path", "https://workspace.example?token=secret",
                                 "https://workspace.example:invalid", "https://workspace.example:8080", ""])
def test_invalid_origins_are_rejected_without_echoing_input(host):
    with pytest.raises(ConfigurationError) as exc:
        DatabricksConfig.from_env(_env(DATABRICKS_HOST=host))
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("path", ["/api/query", "/sql/1.0/warehouses/a?secret=x", ""])
def test_only_warehouse_endpoints_are_accepted(path):
    with pytest.raises(ConfigurationError, match="HTTP_PATH"):
        DatabricksConfig.from_env(_env(DATABRICKS_HTTP_PATH=path))


def test_cli_is_explicit_and_requires_no_password_or_service_secret():
    config = DatabricksConfig.from_env(_env(DATABRICKS_AUTH_TYPE="databricks-cli", DATABRICKS_CLIENT_SECRET="",
                                           DATABRICKS_CONFIG_PROFILE="development"))
    assert config.profile == "development"
    with pytest.raises(ConfigurationError, match="PROFILE"):
        DatabricksConfig.from_env(_env(DATABRICKS_CONFIG_PROFILE="development"))
    with pytest.raises(ConfigurationError, match="explicitly"):
        DatabricksConfig.from_env(_env(DATABRICKS_AUTH_TYPE="pat"))


@pytest.mark.parametrize("key,value", [("PRECIS_DATABRICKS_SOCKET_TIMEOUT", "nan"),
                                      ("PRECIS_DATABRICKS_SOCKET_TIMEOUT", "0"),
                                      ("PRECIS_DATABRICKS_STATEMENT_TIMEOUT", "0"),
                                      ("PRECIS_DATABRICKS_STATEMENT_TIMEOUT", "1.5"),
                                      ("PRECIS_DATABRICKS_STATEMENT_TIMEOUT", "secret")])
def test_timeouts_are_validated_without_echoing_values(key, value):
    with pytest.raises(ConfigurationError) as exc:
        DatabricksConfig.from_env(_env(**{key: value}))
    assert "secret" not in str(exc.value)


def test_names_are_quoted_as_individual_components():
    assert quote_identifier("semantic") == "`semantic`"
    for value in ["a.b", "a`b", "table;DROP", "", "name with spaces"]:
        with pytest.raises(ConfigurationError):
            quote_identifier(value)
