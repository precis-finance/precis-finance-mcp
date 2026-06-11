# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for precis_mcp/backup/manifest.py — checksums, JSON round-trip,
and the git-binary-free instance SHA parser."""
from __future__ import annotations

import hashlib

from precis_mcp.backup.manifest import (
    ArtifactEntry,
    BackupManifest,
    read_instance_git_sha,
    sha256_file,
)


def _manifest() -> BackupManifest:
    return BackupManifest(
        run_id="20260610T023000Z",
        created_at="2026-06-10T02:30:00+00:00",
        mode="dump",
        trigger="cli",
        package_version="0.1.0",
        instance_git_sha="abc123",
        scopes={"postgres": "managed", "clickhouse": "managed", "files": "external"},
        artifacts=[
            ArtifactEntry(store="postgres", key="pg/20260610T023000Z.dump",
                          sha256="aa", size_bytes=10, outcome="success"),
            ArtifactEntry(store="files", key=None, sha256=None,
                          size_bytes=0, outcome="skipped", detail="scope: external"),
        ],
        row_counts={"pg.users": 3, "ch.live.fact_gl": 100},
        outcome="success",
    )


def test_json_round_trip():
    m = _manifest()
    again = BackupManifest.from_json(m.to_json())
    assert again == m
    assert again.artifacts[0].store == "postgres"
    assert again.row_counts["ch.live.fact_gl"] == 100


def test_artifact_lookup():
    m = _manifest()
    assert m.artifact("postgres").key == "pg/20260610T023000Z.dump"
    assert m.artifact("clickhouse") is None


def test_sha256_file(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"precis")
    assert sha256_file(path) == hashlib.sha256(b"precis").hexdigest()


def test_instance_sha_from_ref(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("deadbeef" * 5 + "\n")
    assert read_instance_git_sha(tmp_path) == "deadbeef" * 5


def test_instance_sha_detached_head(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("cafebabe" * 5 + "\n")
    assert read_instance_git_sha(tmp_path) == "cafebabe" * 5


def test_instance_sha_from_packed_refs(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'feedface' * 5} refs/heads/main\n"
    )
    assert read_instance_git_sha(tmp_path) == "feedface" * 5


def test_instance_sha_none_without_git(tmp_path):
    assert read_instance_git_sha(tmp_path) is None
