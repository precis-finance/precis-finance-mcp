# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure URL/DNS admission checks; no network connections are opened."""
from unittest.mock import patch
import pytest
from precis_mcp.artifacts.host_import import validated_target


@pytest.fixture(autouse=True)
def allowed(monkeypatch):
    monkeypatch.setenv("PRECIS_FILE_IMPORT_HOSTS", "files.example")


@pytest.mark.parametrize("url", ["http://files.example/x", "https://other.example/x", "https://files.example:8443/x", "https://user:pass@files.example/x", "https://files.example/x#fragment", "https://files.example:not-a-port/x", "https://files.example/x\n", "https://files.example/é"])
def test_untrusted_destinations_rejected_before_dns(url):
    with patch("precis_mcp.artifacts.host_import.socket.getaddrinfo") as resolver:
        with pytest.raises(ValueError):
            validated_target(url)
        resolver.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_private_addresses_rejected(address):
    with patch("precis_mcp.artifacts.host_import.socket.getaddrinfo", return_value=[(None,None,None,None,(address,443))]):
        with pytest.raises(ValueError):
            validated_target("https://files.example/x")


def test_connection_target_pins_public_ip_and_preserves_tls_host():
    with patch("precis_mcp.artifacts.host_import.socket.getaddrinfo", return_value=[(None,None,None,None,("8.8.8.8",443))]):
        assert validated_target("https://files.example/download?token=opaque") == ("files.example", "8.8.8.8", "/download?token=opaque")
