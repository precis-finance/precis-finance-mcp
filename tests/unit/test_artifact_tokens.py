# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure purpose/version-bound artifact capability tests."""
import jwt
import pytest
from precis_mcp.artifacts import tokens

@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("ARTIFACT_SIGNING_KEY", "test-artifact-key-" + "x" * 32)
    monkeypatch.setenv("PRECIS_BASE_URL", "https://precis.example")


def test_capability_roundtrip_binds_owner_version_and_hash():
    token = tokens.make_token("download", "alice", "file_abc", version=2, sha256="digest")
    claims = tokens.verify_token("download", token)
    assert (claims["sub"], claims["object_id"], claims["version"], claims["sha256"]) == ("alice", "file_abc", 2, "digest")


def test_upload_capability_cannot_authorize_download():
    token = tokens.make_token("upload", "alice", "upload_abc", size_bytes=10)
    with pytest.raises(jwt.InvalidTokenError):
        tokens.verify_token("download", token)


def test_key_rotation_revokes_existing_capabilities(monkeypatch):
    token = tokens.make_token("download", "alice", "file_abc")
    monkeypatch.setenv("ARTIFACT_SIGNING_KEY", "new-key-" + "y" * 32)
    with pytest.raises(jwt.InvalidSignatureError):
        tokens.verify_token("download", token)


@pytest.mark.parametrize("ttl", [0, -1, 901])
def test_transfer_lifetime_is_bounded(ttl):
    with pytest.raises(ValueError):
        tokens.make_token("upload", "alice", "upload_abc", ttl_seconds=ttl)


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ARTIFACT_SIGNING_KEY")
    with pytest.raises(RuntimeError):
        tokens.make_token("download", "alice", "file_abc")
