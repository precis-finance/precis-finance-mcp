# SPDX-License-Identifier: Elastic-2.0
"""Purpose-separated transfer capabilities; never reuse OAuth access tokens."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import jwt
from precis_mcp import oidc

DEFAULT_TTL_SECONDS = 900


def require_configuration() -> str:
    master = os.getenv("ARTIFACT_SIGNING_KEY", "")
    if len(master.encode()) < 32:
        raise RuntimeError("ARTIFACT_SIGNING_KEY must contain at least 32 bytes")
    base = oidc.public_base()
    if not base:
        raise RuntimeError("PRECIS_BASE_URL is required for artifact transfer")
    from urllib.parse import urlsplit
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Invalid artifact public base URL")
    if parsed.scheme != "https" and os.getenv("PRECIS_AUTH_MODE") != "devkey" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Public artifact transfer requires HTTPS")
    return base.rstrip("/")


def _key(purpose: str) -> bytes:
    require_configuration()
    if purpose not in {"download", "upload"}:
        raise ValueError("Invalid transfer purpose")
    return hmac.new(os.environ["ARTIFACT_SIGNING_KEY"].encode(), f"precis-artifact-{purpose}-v1".encode(), hashlib.sha256).digest()


def make_token(purpose: str, user_id: str, object_id: str, *, version=None, sha256=None, size_bytes=None, ttl_seconds=DEFAULT_TTL_SECONDS) -> str:
    if ttl_seconds <= 0 or ttl_seconds > DEFAULT_TTL_SECONDS:
        raise ValueError("Invalid transfer lifetime")
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "object_id": object_id, "aud": f"precis-artifact-{purpose}-v1", "iat": now,
                       "exp": now + timedelta(seconds=ttl_seconds), "version": version, "sha256": sha256,
                       "size_bytes": size_bytes}, _key(purpose), algorithm="HS256")


def verify_token(purpose: str, token: str) -> dict:
    return jwt.decode(token, _key(purpose), algorithms=["HS256"], audience=f"precis-artifact-{purpose}-v1",
                      options={"require": ["exp", "iat", "sub", "object_id", "aud"]})


def transfer_url(purpose: str, user_id: str, object_id: str, **claims) -> str:
    token = make_token(purpose, user_id, object_id, **claims)
    return f"{require_configuration()}/api/artifacts/{purpose}?t={token}"


def artifact_descriptor(row: dict) -> dict:
    now = datetime.now(timezone.utc)
    ttl = DEFAULT_TTL_SECONDS
    expiry = row.get("expires_at")
    if row.get("expires_at"):
        expiry = datetime.fromisoformat(row["expires_at"]) if isinstance(row["expires_at"], str) else row["expires_at"]
        ttl = min(ttl, int((expiry - now).total_seconds()))
    url = transfer_url("download", row["user_id"], row["file_id"], version=row["version"], sha256=row["sha256"], ttl_seconds=ttl)
    return {"type": "artifact", "artifact_type": "file", "file_id": row["file_id"], "basename": row["basename"],
            "filename": row["basename"], "mime_type": row["mime_type"], "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "version": row["version"], "expires_at": expiry.isoformat() if expiry else None,
            "download_url": url, "download_expires_at": (now + timedelta(seconds=ttl)).isoformat(), "display": "card"}
