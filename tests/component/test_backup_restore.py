# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Component tests for precis_mcp/backup/restore.py — safety guards, checksum
verification, and the drill flow against the shared fakes. The Postgres
helpers are module-level seams monkeypatched here (the real ones need a live
server; the drill's correctness is the orchestration around them)."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from precis_mcp.backup import (
    BackupExecutionError,
    BundleNotFoundError,
    RestoreGuardError,
)
from precis_mcp.backup import restore as restore_mod
from precis_mcp.backup.config import (
    BackupConfig,
    CredentialRefs,
    DestinationCfg,
    RetentionCfg,
)
from precis_mcp.backup.destination import LocalDestination
from precis_mcp.backup.manifest import ArtifactEntry, BackupManifest, manifest_key
from precis_mcp.backup.restore import restore_bundle
from tests.fakes.fake_platform_db import FakePlatformDB

RUN_ID = "20260610T023000Z"


def _cfg(tmp_path) -> BackupConfig:
    return BackupConfig(
        mode="dump",
        destination=DestinationCfg(type="local", path=str(tmp_path / "dest")),
        credentials=CredentialRefs(),
        schedule_cron="30 2 * * *",
        retention={"postgres": RetentionCfg(keep=7)},
        scope={"postgres": "managed", "clickhouse": "managed", "files": "external"},
    )


def _seed_bundle(cfg, *, pg_sha=None, row_counts=None) -> LocalDestination:
    dest = LocalDestination(cfg.destination.path)
    pg_blob = b"pg-dump-sentinel"
    dest.put_bytes(pg_blob, f"pg/{RUN_ID}.dump")
    dest.put_bytes(b"ch-backup-sentinel", f"ch/{RUN_ID}.zip")
    manifest = BackupManifest(
        run_id=RUN_ID, created_at="2026-06-10T02:30:00+00:00", mode="dump",
        trigger="cli", package_version="t", instance_git_sha=None,
        scopes={"postgres": "managed", "clickhouse": "managed", "files": "external"},
        artifacts=[
            ArtifactEntry(store="postgres", key=f"pg/{RUN_ID}.dump",
                          sha256=pg_sha or hashlib.sha256(pg_blob).hexdigest(),
                          size_bytes=len(pg_blob), outcome="success"),
            ArtifactEntry(store="clickhouse", key=f"ch/{RUN_ID}.zip",
                          sha256=None, size_bytes=18, outcome="success"),
        ],
        row_counts=row_counts or {"pg.users": 3, "ch.live.fact_gl": 100},
        outcome="success",
    )
    dest.put_bytes(manifest.to_json().encode(), manifest_key(RUN_ID))
    return dest


def test_unknown_bundle_raises_not_found(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    LocalDestination(cfg.destination.path)  # empty destination
    with pytest.raises(BundleNotFoundError, match="backup list"):
        restore_bundle(cfg, "20990101T000000Z", ch_client_factory=lambda: ch_client)


def test_nonempty_postgres_target_refused_without_force(tmp_path, ch_client, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg)
    monkeypatch.setattr(restore_mod, "_pg_database_exists", lambda dbname: True)
    monkeypatch.setattr(restore_mod, "_pg_table_count", lambda dbname: 12)
    restored = []
    with pytest.raises(RestoreGuardError, match="--force"):
        restore_bundle(
            cfg, RUN_ID, stores={"postgres"},
            ch_client_factory=lambda: ch_client,
            pg_restore_runner=lambda path, db: restored.append(db),
        )
    assert restored == []


def test_nonempty_clickhouse_refused_without_force(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg)
    ch_client.set_response("system.tables", [("live",)])
    with pytest.raises(RestoreGuardError, match="--force"):
        restore_bundle(cfg, RUN_ID, stores={"clickhouse"},
                       ch_client_factory=lambda: ch_client)
    assert all("RESTORE" not in sql for sql, _ in ch_client.commands)


def test_force_restore_drops_and_restores_clickhouse(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg)
    ch_client.set_response("system.tables", [("live",)])
    result = restore_bundle(cfg, RUN_ID, force=True, stores={"clickhouse"},
                            ch_client_factory=lambda: ch_client)
    assert result.outcome == "success"
    sqls = [sql for sql, _ in ch_client.commands]
    assert "DROP DATABASE IF EXISTS live SYNC" in sqls
    assert (
        "RESTORE DATABASE live, DATABASE staging, DATABASE semantic "
        f"FROM Disk('precis_backups', '{RUN_ID}.zip')"
    ) in sqls


def test_checksum_mismatch_aborts_before_any_restore(tmp_path, ch_client, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg, pg_sha="0" * 64)
    restored = []
    monkeypatch.setattr(restore_mod, "_pg_database_exists", lambda dbname: False)
    with pytest.raises(BackupExecutionError, match="checksum mismatch"):
        restore_bundle(
            cfg, RUN_ID, force=True,
            ch_client_factory=lambda: ch_client,
            pg_restore_runner=lambda path, db: restored.append(db),
        )
    assert restored == []
    assert ch_client.commands == []


def _drill_env(monkeypatch, db: FakePlatformDB, *, pg_counts):
    admin_sql: list[str] = []
    restored: list[str] = []
    monkeypatch.setattr(restore_mod, "_pg_execute_admin", admin_sql.append)
    monkeypatch.setattr(restore_mod, "_pg_table_counts", lambda dbname: pg_counts)
    return admin_sql, restored


def test_drill_restores_into_side_databases_and_verifies(tmp_path, ch_client, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg, row_counts={"pg.users": 3, "ch.live.fact_gl": 100})
    admin_sql, restored = _drill_env(monkeypatch, FakePlatformDB(),
                                     pg_counts={"users": 3})
    ch_client.set_response("_drill", [("live_drill", "fact_gl", 100)])

    db = FakePlatformDB()
    with patch("precis_mcp.db.execute_platform", side_effect=db.execute):
        result = restore_bundle(
            cfg, RUN_ID, drill=True,
            ch_client_factory=lambda: ch_client,
            pg_restore_runner=lambda path, dbname: restored.append(dbname),
        )

    assert result.outcome == "success"
    assert restored == [restore_mod.DRILL_PG_DB]
    # Never plain `RESTORE DATABASE live FROM` — always the AS-rename form.
    sqls = [sql for sql, _ in ch_client.commands]
    restore_sql = next(s for s in sqls if s.startswith("RESTORE"))
    assert "DATABASE live AS live_drill" in restore_sql
    assert "DATABASE semantic AS semantic_drill" in restore_sql
    assert "RESTORE DATABASE live," not in restore_sql
    # Drill DBs dropped afterwards (no --keep-drill).
    assert f'DROP DATABASE IF EXISTS "{restore_mod.DRILL_PG_DB}"' in admin_sql[-1]
    assert "DROP DATABASE IF EXISTS live_drill SYNC" in sqls
    # Verification matched the manifest baseline.
    assert all(v.ok for v in result.verification)
    assert {v.name for v in result.verification} == {"pg.users", "ch.live.fact_gl"}
    # Drill telemetry row (invariant 6).
    assert db.backup_history[0]["kind"] == "restore_drill"
    assert db.backup_history[0]["outcome"] == "success"


def test_drill_row_count_mismatch_fails(tmp_path, ch_client, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg, row_counts={"pg.users": 3, "ch.live.fact_gl": 100})
    _drill_env(monkeypatch, FakePlatformDB(), pg_counts={"users": 1})  # wrong
    ch_client.set_response("_drill", [("live_drill", "fact_gl", 100)])

    db = FakePlatformDB()
    with patch("precis_mcp.db.execute_platform", side_effect=db.execute):
        result = restore_bundle(
            cfg, RUN_ID, drill=True,
            ch_client_factory=lambda: ch_client,
            pg_restore_runner=lambda path, dbname: None,
        )

    assert result.outcome == "failed"
    bad = [v for v in result.verification if not v.ok]
    assert [v.name for v in bad] == ["pg.users"]
    assert db.backup_history[0]["outcome"] == "failed"


def test_keep_drill_skips_drop(tmp_path, ch_client, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_bundle(cfg)
    admin_sql, _ = _drill_env(monkeypatch, FakePlatformDB(), pg_counts={"users": 3})
    ch_client.set_response("_drill", [("live_drill", "fact_gl", 100)])

    with patch("precis_mcp.db.execute_platform", side_effect=FakePlatformDB().execute):
        restore_bundle(
            cfg, RUN_ID, drill=True, keep_drill=True,
            ch_client_factory=lambda: ch_client,
            pg_restore_runner=lambda path, dbname: None,
        )

    # Initial stale-drill drop + create only; no trailing drop.
    assert admin_sql[0].startswith("DROP DATABASE IF EXISTS")
    assert admin_sql[1].startswith("CREATE DATABASE")
    assert len(admin_sql) == 2
    sqls = [sql for sql, _ in ch_client.commands]
    # The leading stale-drill drops happen before RESTORE; none after it.
    restore_idx = next(i for i, s in enumerate(sqls) if s.startswith("RESTORE"))
    assert all(not s.startswith("DROP") for s in sqls[restore_idx + 1:])


def test_instance_restore_extracts_beside_never_in_place(tmp_path, ch_client):
    cfg = _cfg(tmp_path)
    dest = _seed_bundle(cfg)
    # Add an instance artifact to the bundle.
    import io
    import tarfile

    instance_src = tmp_path / "instance-src"
    instance_src.mkdir()
    (instance_src / "scenarios.yml").write_text("scenarios: []\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(instance_src, arcname=".")
    blob = buf.getvalue()
    dest.put_bytes(blob, f"instance/{RUN_ID}.tar.gz")
    manifest = BackupManifest.from_json(dest.get_bytes(manifest_key(RUN_ID)))
    manifest.artifacts.append(ArtifactEntry(
        store="instance", key=f"instance/{RUN_ID}.tar.gz",
        sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob),
        outcome="success",
    ))
    dest.put_bytes(manifest.to_json().encode(), manifest_key(RUN_ID))

    out_dir = tmp_path / "restored-instance"
    result = restore_bundle(
        cfg, RUN_ID, stores={"instance"}, instance_out_dir=out_dir,
        ch_client_factory=lambda: ch_client,
    )
    assert result.outcome == "success"
    assert (out_dir / "scenarios.yml").is_file()
    detail = next(s.detail for s in result.stores if s.store == "instance")
    assert "never overwritten" in detail
