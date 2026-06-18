# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Component tests for precis_mcp/backup/runner.py — one backup run end to
end against a tmp_path local destination, the shared ClickHouse fake, and the
in-memory platform DB."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from precis_mcp.backup.config import (
    BackupConfig,
    CredentialRefs,
    DestinationCfg,
    RetentionCfg,
)
from precis_mcp.backup.destination import LocalDestination
from precis_mcp.backup.manifest import ArtifactEntry, BackupManifest, manifest_key
from precis_mcp.backup.runner import run_backup
from tests.fakes.fake_platform_db import FakePlatformDB

NOW = datetime(2026, 6, 10, 2, 30, 0, tzinfo=timezone.utc)
RUN_ID = "20260610T023000Z"


def _cfg(tmp_path, *, scope=None, webhook=None) -> BackupConfig:
    return BackupConfig(
        mode="dump",
        destination=DestinationCfg(type="local", path=str(tmp_path / "dest")),
        credentials=CredentialRefs(),
        schedule_cron="30 2 * * *",
        retention={"postgres": RetentionCfg(keep=7), "clickhouse": RetentionCfg(keep=7),
                   "instance": RetentionCfg(keep=7)},
        scope=scope or {"postgres": "managed", "clickhouse": "managed", "files": "external"},
        alert_webhook_url=webhook,
    )


def _instance_dir(tmp_path) -> Path:
    instance = tmp_path / "instance"
    (instance / "catalogue").mkdir(parents=True)
    (instance / "catalogue" / "pnl.yml").write_text("metrics: []\n")
    git = instance / ".git"
    git.mkdir()
    (git / "HEAD").write_text("abc123def" * 4 + "abcd" + "\n")
    return instance


def _ok_pg_dump(out_path: Path) -> None:
    out_path.write_bytes(b"pg-dump-sentinel")


def _run(cfg, ch_client, db, *, pg_dump=_ok_pg_dump, instance_dir=None):
    """run_backup with the CH artifact pre-seeded (the real server writes it
    server-side through the backup disk; the fake client cannot)."""
    dest_root = Path(cfg.destination.path)
    if cfg.scope.get("clickhouse") == "managed":
        (dest_root / "ch").mkdir(parents=True, exist_ok=True)
        (dest_root / "ch" / f"{RUN_ID}.zip").write_bytes(b"ch-backup-sentinel")
    with patch("precis_mcp.db.query_platform", side_effect=db.query), \
         patch("precis_mcp.db.execute_platform", side_effect=db.execute):
        return run_backup(
            cfg,
            trigger="cli",
            instance_dir=instance_dir,
            ch_client_factory=lambda: ch_client,
            pg_dump_runner=pg_dump,
            now=NOW,
        )


def test_successful_run_produces_complete_bundle(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    db = FakePlatformDB()
    result = _run(cfg, ch_client, db, instance_dir=_instance_dir(tmp_path))

    assert result.outcome == "success"
    assert result.run_id == RUN_ID

    sql = ch_client.commands[0][0]
    assert sql == (
        "BACKUP DATABASE live, DATABASE staging, DATABASE semantic "
        f"TO Disk('precis_backups', '{RUN_ID}.zip')"
    )

    dest = LocalDestination(cfg.destination.path)
    keys = dest.list_keys()
    assert f"pg/{RUN_ID}.dump" in keys
    assert f"ch/{RUN_ID}.zip" in keys
    assert f"instance/{RUN_ID}.tar.gz" in keys
    assert manifest_key(RUN_ID) in keys

    manifest = BackupManifest.from_json(dest.get_bytes(manifest_key(RUN_ID)))
    assert manifest.outcome == "success"
    assert manifest.instance_git_sha == "abc123def" * 4 + "abcd"
    assert manifest.scopes["files"] == "external"
    pg = manifest.artifact("postgres")
    assert pg.outcome == "success" and pg.sha256 and pg.size_bytes > 0
    ch = manifest.artifact("clickhouse")
    assert ch.outcome == "success" and ch.sha256  # local destination → checksummed
    assert manifest.artifact("files").outcome == "skipped"


def test_history_row_recorded_best_effort(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    db = FakePlatformDB()
    _run(cfg, ch_client, db, instance_dir=_instance_dir(tmp_path))

    assert len(db.backup_history) == 1
    row = db.backup_history[0]
    assert row["run_id"] == RUN_ID
    assert row["kind"] == "backup"
    assert row["outcome"] == "success"
    assert row["artifacts"]["postgres"]["outcome"] == "success"


def test_history_write_failure_does_not_fail_run(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    instance = _instance_dir(tmp_path)
    dest_root = Path(cfg.destination.path)
    (dest_root / "ch").mkdir(parents=True, exist_ok=True)
    (dest_root / "ch" / f"{RUN_ID}.zip").write_bytes(b"x")
    with patch("precis_mcp.db.query_platform", side_effect=RuntimeError("pg down")), \
         patch("precis_mcp.db.execute_platform", side_effect=RuntimeError("pg down")):
        result = run_backup(
            cfg, trigger="cli", instance_dir=instance,
            ch_client_factory=lambda: ch_client,
            pg_dump_runner=_ok_pg_dump, now=NOW,
        )
    assert result.outcome == "success"


def test_store_failure_degrades_to_partial_and_fires_webhook(tmp_path, ch_client):
    def failing_pg_dump(out_path: Path) -> None:
        raise RuntimeError("connection refused")

    cfg = _cfg(tmp_path, webhook="https://hooks.example.com/x")
    db = FakePlatformDB()
    with patch("httpx.post") as post:
        result = _run(cfg, ch_client, db, pg_dump=failing_pg_dump,
                      instance_dir=_instance_dir(tmp_path))

    assert result.outcome == "partial"
    assert result.failed_stores == ["postgres"]
    post.assert_called_once()
    payload = post.call_args.kwargs["json"]
    assert payload["run_id"] == RUN_ID
    assert payload["failed_stores"] == ["postgres"]

    # The bundle still lands, manifest records the failure per artifact.
    dest = LocalDestination(cfg.destination.path)
    manifest = BackupManifest.from_json(dest.get_bytes(manifest_key(RUN_ID)))
    assert manifest.outcome == "partial"
    assert manifest.artifact("postgres").outcome == "failed"
    assert manifest.artifact("clickhouse").outcome == "success"
    assert db.backup_history[0]["outcome"] == "partial"


def test_external_scope_skips_store_with_notice(tmp_path, ch_client):
    cfg = _cfg(tmp_path, scope={"postgres": "managed", "clickhouse": "external",
                                "files": "external"})
    db = FakePlatformDB()
    result = _run(cfg, ch_client, db, instance_dir=_instance_dir(tmp_path))

    assert result.outcome == "success"
    assert ch_client.commands == []  # external store: no BACKUP issued
    ch = next(s for s in result.stores if s.store == "clickhouse")
    assert ch.outcome == "skipped"
    assert "customer-managed" in ch.detail


def test_run_prunes_old_bundles_but_protects_current(tmp_path, ch_client):
    import dataclasses

    cfg = dataclasses.replace(
        _cfg(tmp_path),
        retention={"postgres": RetentionCfg(keep=1), "clickhouse": RetentionCfg(keep=1),
                   "instance": RetentionCfg(keep=1)},
    )
    dest = LocalDestination(cfg.destination.path)
    dest.put_bytes(b"old", "pg/20260101T000000Z.dump")
    old = BackupManifest(
        run_id="20260101T000000Z", created_at="2026-01-01T00:00:00+00:00",
        mode="dump", trigger="cli", package_version="t", instance_git_sha=None,
        scopes={},
        artifacts=[ArtifactEntry(store="postgres", key="pg/20260101T000000Z.dump",
                                 sha256="aa", size_bytes=3, outcome="success")],
        outcome="success",
    )
    dest.put_bytes(old.to_json().encode(), manifest_key(old.run_id))

    db = FakePlatformDB()
    _run(cfg, ch_client, db, instance_dir=_instance_dir(tmp_path))

    keys = dest.list_keys()
    assert "pg/20260101T000000Z.dump" not in keys  # pruned (keep=1, newer run exists)
    assert f"pg/{RUN_ID}.dump" in keys  # current run protected
