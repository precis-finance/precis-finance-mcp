# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Backup manifest — the JSON record that binds a bundle's artifacts together.

Pushed to ``manifest/<run_id>.json`` *after* every artifact has landed, so
manifest presence at the destination means the bundle is complete. Carries
per-artifact checksums (drill/restore verification), per-table row counts
(the drill success baseline), and the instance git SHA so a restore can
assert which instance state the bundle belongs to.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ArtifactEntry:
    store: str
    key: str | None  # None when the store was skipped or the artifact failed
    sha256: str | None  # None for server-side-pushed s3 CH artifacts
    size_bytes: int
    outcome: str  # 'success' | 'failed' | 'skipped'
    detail: str | None = None


@dataclass(frozen=True)
class BackupManifest:
    run_id: str
    created_at: str
    mode: str
    trigger: str
    package_version: str
    instance_git_sha: str | None
    scopes: dict[str, str]
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    outcome: str = "success"

    def artifact(self, store: str) -> ArtifactEntry | None:
        for entry in self.artifacts:
            if entry.store == store:
                return entry
        return None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "BackupManifest":
        data = json.loads(raw)
        data["artifacts"] = [ArtifactEntry(**a) for a in data.get("artifacts", [])]
        return cls(**data)


def manifest_key(run_id: str) -> str:
    return f"manifest/{run_id}.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("precis-mcp")
    except Exception:
        return "unknown"


def read_instance_git_sha(instance_dir: Path) -> str | None:
    """Resolve the instance checkout's HEAD SHA without a git binary
    (the runtime image ships none)."""
    git_dir = Path(instance_dir) / ".git"
    if git_dir.is_file():
        # Worktree/submodule indirection: `.git` is a file with `gitdir: <path>`.
        line = git_dir.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            git_dir = (Path(instance_dir) / line.split(":", 1)[1].strip()).resolve()
    if not git_dir.is_dir():
        return None
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(":", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.is_file():
        return ref_file.read_text(encoding="utf-8").strip() or None
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0]
    return None
