# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for precis_mcp/backup/prune.py — retention against a local
destination over tmp_path (LocalDestination is pure pathlib; no I/O clients)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from precis_mcp.backup.config import (
    BackupConfig,
    CredentialRefs,
    DestinationCfg,
    RetentionCfg,
)
from precis_mcp.backup.destination import LocalDestination, WormDeleteDenied
from precis_mcp.backup.manifest import ArtifactEntry, BackupManifest, manifest_key
from precis_mcp.backup.prune import prune_bundles

NOW = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)


def _cfg(retention: dict[str, RetentionCfg], *, expect_worm: bool = False) -> BackupConfig:
    return BackupConfig(
        mode="dump",
        destination=DestinationCfg(type="local", path="/unused"),
        credentials=CredentialRefs(),
        schedule_cron="30 2 * * *",
        retention=retention,
        scope={"postgres": "managed", "clickhouse": "managed", "files": "external"},
        expect_worm=expect_worm,
    )


def _seed_bundle(dest: LocalDestination, age_days: int, *, stores=("postgres", "clickhouse")) -> str:
    created = NOW - timedelta(days=age_days)
    run_id = created.strftime("%Y%m%dT%H%M%SZ")
    artifacts = []
    for store in stores:
        prefix = {"postgres": "pg", "clickhouse": "ch", "instance": "instance"}[store]
        ext = {"pg": "dump", "ch": "zip", "instance": "tar.gz"}[prefix]
        key = f"{prefix}/{run_id}.{ext}"
        dest.put_bytes(b"x", key)
        artifacts.append(ArtifactEntry(store=store, key=key, sha256="aa",
                                       size_bytes=1, outcome="success"))
    manifest = BackupManifest(
        run_id=run_id, created_at=created.isoformat(), mode="dump", trigger="cli",
        package_version="t", instance_git_sha=None,
        scopes={}, artifacts=artifacts, outcome="success",
    )
    dest.put_bytes(manifest.to_json().encode(), manifest_key(run_id))
    return run_id


def test_keep_n_prunes_oldest(tmp_path):
    dest = LocalDestination(tmp_path)
    runs = [_seed_bundle(dest, age) for age in (0, 1, 2, 3)]
    cfg = _cfg({"postgres": RetentionCfg(keep=2), "clickhouse": RetentionCfg(keep=2)})

    report = prune_bundles(cfg, dest, now=NOW)

    keys = dest.list_keys()
    assert f"pg/{runs[0]}.dump" in keys and f"pg/{runs[1]}.dump" in keys
    assert f"pg/{runs[2]}.dump" not in keys and f"pg/{runs[3]}.dump" not in keys
    # Fully-pruned bundles lose their manifest too.
    assert manifest_key(runs[3]) not in keys
    assert manifest_key(runs[0]) in keys
    assert report.worm_suppressed == 0


def test_days_bound_intersects_keep(tmp_path):
    dest = LocalDestination(tmp_path)
    fresh = _seed_bundle(dest, 1)
    stale = _seed_bundle(dest, 40)
    cfg = _cfg({"postgres": RetentionCfg(keep=10, days=30),
                "clickhouse": RetentionCfg(keep=10, days=30)})

    prune_bundles(cfg, dest, now=NOW)

    keys = dest.list_keys()
    assert f"pg/{fresh}.dump" in keys
    assert f"pg/{stale}.dump" not in keys
    assert manifest_key(stale) not in keys


def test_manifest_survives_while_any_artifact_remains(tmp_path):
    dest = LocalDestination(tmp_path)
    runs = [_seed_bundle(dest, age) for age in (0, 1, 2)]
    # Only postgres has retention: clickhouse artifacts are never pruned, so
    # the old bundle keeps its manifest even after its pg dump is gone.
    cfg = _cfg({"postgres": RetentionCfg(keep=1)})

    prune_bundles(cfg, dest, now=NOW)

    keys = dest.list_keys()
    assert f"pg/{runs[2]}.dump" not in keys
    assert f"ch/{runs[2]}.zip" in keys
    assert manifest_key(runs[2]) in keys


def test_protected_run_never_pruned(tmp_path):
    dest = LocalDestination(tmp_path)
    runs = [_seed_bundle(dest, age) for age in (0, 5)]
    cfg = _cfg({"postgres": RetentionCfg(days=1), "clickhouse": RetentionCfg(days=1)})

    prune_bundles(cfg, dest, protect_run_id=runs[1], now=NOW)

    assert f"pg/{runs[1]}.dump" in dest.list_keys()


class _WormDestination(LocalDestination):
    def delete(self, key: str) -> None:
        raise WormDeleteDenied("object locked")


def test_worm_denied_deletes_warn_once_and_keep_manifest(tmp_path, caplog):
    dest = _WormDestination(tmp_path)
    runs = [_seed_bundle(dest, age) for age in (0, 1, 2)]
    cfg = _cfg({"postgres": RetentionCfg(keep=1), "clickhouse": RetentionCfg(keep=1)})

    with caplog.at_level(logging.WARNING, logger="precis_mcp.backup.prune"):
        report = prune_bundles(cfg, dest, now=NOW)

    assert report.worm_suppressed == 4  # 2 stores × 2 doomed runs
    assert report.deleted == []
    assert manifest_key(runs[2]) in dest.list_keys()
    worm_warnings = [r for r in caplog.records if "WORM" in r.message]
    assert len(worm_warnings) == 1


def test_worm_denied_is_info_when_expected(tmp_path, caplog):
    dest = _WormDestination(tmp_path)
    for age in (0, 1):
        _seed_bundle(dest, age)
    cfg = _cfg({"postgres": RetentionCfg(keep=1)}, expect_worm=True)

    with caplog.at_level(logging.INFO, logger="precis_mcp.backup.prune"):
        prune_bundles(cfg, dest, now=NOW)

    worm_records = [r for r in caplog.records if "WORM" in r.message]
    assert worm_records and all(r.levelno == logging.INFO for r in worm_records)
