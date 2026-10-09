# SPDX-License-Identifier: Elastic-2.0
"""Optional host attachment import with bounded, DNS-pinned HTTPS downloads."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import ipaddress
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

from precis_mcp.artifacts import registry


def allowed_hosts() -> set[str]:
    return {host.strip().lower() for host in os.getenv("PRECIS_FILE_IMPORT_HOSTS", "").split(",") if host.strip()}


def validated_target(url: str) -> tuple[str, str, str]:
    if not isinstance(url, str) or len(url) > 8192 or not url.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid attachment URL")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid attachment URL") from None
    if (parsed.scheme != "https" or port not in {None, 443} or parsed.username or parsed.password
        or parsed.fragment or host not in allowed_hosts()):
        raise ValueError("Attachment URL must use an explicitly allowed HTTPS host")
    addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Attachment host must resolve only to public addresses")
    # Connect to this validated address, preserving TLS verification and Host.
    # A second DNS lookup cannot rebind the destination to an internal service.
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return host, sorted(addresses)[0], target


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def import_attachment(file: dict, user_id: str) -> dict:
    if not isinstance(file, dict) or not isinstance(file.get("file_id"), str) or not file["file_id"]:
        raise ValueError("Host file_id is required")
    filename = file.get("file_name")
    if not filename:
        raise ValueError("Host attachment filename is required to choose a supported file format")
    registry._validate_basename(filename)
    mime = registry.validate_mime_type(file.get("mime_type") or "application/octet-stream")
    host, address, target = validated_target(file.get("download_url", ""))
    # URLs are transient host capabilities: never persist them as provenance,
    # forward OAuth credentials, follow redirects, or log the request URL.
    connection = _PinnedHTTPS(host, address)
    try:
        connection.request("GET", target, headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("Attachment download failed")
        declared = response.getheader("Content-Length")
        if declared and (not declared.isdecimal() or int(declared) > registry._MAX_FILE_BYTES):
            raise registry.FileTooLarge("Attachment exceeds 10 MiB")
        data = bytearray()
        deadline = time.monotonic() + 60
        while True:
            if time.monotonic() > deadline:
                raise ValueError("Attachment transfer timed out")
            chunk = response.read(min(65536, registry._MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > registry._MAX_FILE_BYTES:
                raise registry.FileTooLarge("Attachment exceeds 10 MiB")
        if not data or (declared and len(data) != int(declared)):
            raise ValueError("Attachment size mismatch")

    except (OSError, http.client.HTTPException):
        # Protocol exceptions can include a request target containing the host
        # capability. Return a fixed message before the tool's error logger.
        raise ValueError("Attachment download failed") from None
    finally:
        connection.close()
    return registry.create_file(user_id=user_id, basename=filename, blob_bytes=bytes(data), source="user_upload",
                                mime_type=mime, expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                                provenance={"host": "chatgpt", "host_file_id": file["file_id"]})
