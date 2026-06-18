# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""One backup run — per-store runners and the orchestrator.

A store failure never aborts the run: the remaining stores are still
attempted, the manifest records per-artifact outcomes, and the run outcome
degrades to ``partial`` (or ``failed`` when nothing succeeded). The manifest
is pushed last — its presence at the destination is the bundle-complete
signal. Transport split: ClickHouse pushes its own artifact server-side
through the rendered backup disk; everything else is pushed by this process.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from precis_mcp.backup import BackupExecutionError
from precis_mcp.backup.ch_render import BACKUP_DISK_NAME, CH_PREFIX
from precis_mcp.backup.config import BackupConfig
from precis_mcp.backup.destination import BackupDestination, LocalDestination, build_destination
from precis_mcp.backup.history import record_history
from precis_mcp.backup.manifest import (
    ArtifactEntry,
    BackupManifest,
    manifest_key,
    package_version,
    read_instance_git_sha,
    sha256_file,
)
from precis_mcp.backup.notify import post_failure
from precis_mcp.backup.prune import prune_bundles

logger = logging.getLogger(__name__)

CH_DATABASES = ("live", "staging", "semantic")


@dataclass
class StoreResult:
    store: str
    key: str | None = None
    sha256: str | None = None
    size_bytes: int = 0
    outcome: str = "success"  # 'success' | 'failed' | 'skipped'
    detail: str | None = None


@dataclass
class BackupResult:
    run_id: str
    mode: str
    trigger: str
    outcome: str
    stores: list[StoreResult] = field(default_factory=list)
    manifest_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def failed_stores(self) -> list[str]:
        return [s.store for s in self.stores if s.outcome == "failed"]


def backup_ch_client():
    """A ClickHouse client for BACKUP/RESTORE — env-derived settings plus a
    long send/receive timeout (a full backup can exceed the HTTP default)."""
    from precis_mcp.db import get_clickhouse_client

    timeout = int(os.getenv("PRECIS_BACKUP_CH_TIMEOUT", "3600"))
    return get_clickhouse_client(send_receive_timeout=timeout)


def _run_pg_dump(out_path: Path) -> None:
    cmd = [
        "pg_dump",
        "--format=custom",
        "-h", os.getenv("PGHOST", "localhost"),
        "-p", os.getenv("PGPORT", "5432"),
        "-U", os.getenv("PGUSER", "postgres"),
        "-d", os.getenv("PLATFORM_DB_NAME", "precis_platform"),
        "--file", str(out_path),
    ]
    proc = subprocess.run(
        cmd, env=os.environ.copy(), capture_output=True, text=True, timeout=3600
    )
    if proc.returncode != 0:
        raise BackupExecutionError(f"pg_dump failed: {proc.stderr.strip()[-500:]}")


def _tar_dir(src: Path, out_path: Path, *, exclude_git: bool = False) -> None:
    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if exclude_git and (info.name == ".git" or info.name.startswith(".git/")):
            return None
        return info

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(src, arcname=".", filter=_filter)


def _push(dest: BackupDestination, local: Path, key: str) -> tuple[str, int]:
    dest.put_file(local, key)
    return sha256_file(local), local.stat().st_size


def _backup_postgres(
    cfg: BackupConfig,
    dest: BackupDestination,
    run_id: str,
    workdir: Path,
    pg_dump_runner: Callable[[Path], None],
) -> StoreResult:
    if cfg.scope.get("postgres") == "external":
        return StoreResult(
            store="postgres", outcome="skipped",
            detail="scope: external — customer-managed store; back it up with your own tooling",
        )
    try:
        dump = workdir / f"{run_id}.dump"
        pg_dump_runner(dump)
        key = f"pg/{run_id}.dump"
        sha, size = _push(dest, dump, key)
        return StoreResult(store="postgres", key=key, sha256=sha, size_bytes=size)
    except Exception as exc:
        logger.exception("backup: postgres store failed (run_id=%s)", run_id)
        return StoreResult(store="postgres", outcome="failed", detail=str(exc))


def _backup_clickhouse(
    cfg: BackupConfig, dest: BackupDestination, run_id: str, ch_client
) -> StoreResult:
    if cfg.scope.get("clickhouse") == "external":
        return StoreResult(
            store="clickhouse", outcome="skipped",
            detail="scope: external — customer-managed store; back it up with your own tooling",
        )
    artifact = f"{run_id}.zip"
    key = f"{CH_PREFIX}/{artifact}"
    databases = ", ".join(f"DATABASE {db}" for db in CH_DATABASES)
    try:
        ch_client.command(f"BACKUP {databases} TO Disk('{BACKUP_DISK_NAME}', '{artifact}')")
        size = dest.size_of(key)
        sha = None
        if isinstance(dest, LocalDestination):
            sha = sha256_file(dest.root / key)
        return StoreResult(store="clickhouse", key=key, sha256=sha, size_bytes=size)
    except Exception as exc:
        logger.exception("backup: clickhouse store failed (run_id=%s)", run_id)
        return StoreResult(store="clickhouse", outcome="failed", detail=str(exc))


def _backup_instance(
    dest: BackupDestination, run_id: str, workdir: Path, instance_dir: Path
) -> StoreResult:
    try:
        tarball = workdir / f"instance-{run_id}.tar.gz"
        _tar_dir(instance_dir, tarball, exclude_git=True)
        key = f"instance/{run_id}.tar.gz"
        sha, size = _push(dest, tarball, key)
        return StoreResult(store="instance", key=key, sha256=sha, size_bytes=size)
    except Exception as exc:
        logger.exception("backup: instance store failed (run_id=%s)", run_id)
        return StoreResult(store="instance", outcome="failed", detail=str(exc))


def _backup_files(cfg: BackupConfig, dest: BackupDestination, run_id: str, workdir: Path) -> StoreResult:
    if cfg.scope.get("files") != "managed":
        return StoreResult(
            store="files", outcome="skipped",
            detail="scope: external — user files are not on managed local disk",
        )
    user_data_dir = Path(os.getenv("USER_DATA_DIR", "/data/users"))
    if not user_data_dir.is_dir():
        return StoreResult(
            store="files", outcome="failed",
            detail=f"USER_DATA_DIR {user_data_dir} does not exist",
        )
    try:
        tarball = workdir / f"files-{run_id}.tar.gz"
        _tar_dir(user_data_dir, tarball)
        key = f"files/{run_id}.tar.gz"
        sha, size = _push(dest, tarball, key)
        return StoreResult(store="files", key=key, sha256=sha, size_bytes=size)
    except Exception as exc:
        logger.exception("backup: files store failed (run_id=%s)", run_id)
        return StoreResult(store="files", outcome="failed", detail=str(exc))


def _collect_row_counts(cfg: BackupConfig, ch_client) -> dict[str, int]:
    """The drill-verification baseline. Best-effort: a failure here degrades
    the drill's depth, never the backup."""
    counts: dict[str, int] = {}
    if cfg.scope.get("postgres") == "managed":
        try:
            from precis_mcp import db

            tables = db.query_platform(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            for row in tables:
                name = row["table_name"]
                got = db.query_platform(f'SELECT count(*) AS n FROM "{name}"')
                counts[f"pg.{name}"] = int(got[0]["n"]) if got else 0
        except Exception:
            logger.warning("backup: postgres row-count baseline failed", exc_info=True)
    if cfg.scope.get("clickhouse") == "managed" and ch_client is not None:
        try:
            dbs = ", ".join(f"'{db}'" for db in CH_DATABASES)
            result = ch_client.query(
                "SELECT database, name, total_rows FROM system.tables "
                f"WHERE database IN ({dbs}) AND engine NOT LIKE '%View%'"
            )
            for database, name, total_rows in result.result_rows:
                counts[f"ch.{database}.{name}"] = int(total_rows or 0)
        except Exception:
            logger.warning("backup: clickhouse row-count baseline failed", exc_info=True)
    return counts


def run_backup(
    cfg: BackupConfig,
    *,
    trigger: str,
    instance_dir: Path | None = None,
    ch_client_factory: Callable[[], object] | None = None,
    dest_factory: Callable[[], BackupDestination] | None = None,
    pg_dump_runner: Callable[[Path], None] | None = None,
    now: datetime | None = None,
) -> BackupResult:
    started = now or datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    logger.info("backup: run start (run_id=%s trigger=%s)", run_id, trigger)

    if instance_dir is None:
        from precis_mcp.clickhouse_init import default_instance_dir

        instance_dir = default_instance_dir()

    dest = dest_factory() if dest_factory else build_destination(cfg, credential="writer")

    ch_client = None
    if cfg.scope.get("clickhouse") == "managed":
        try:
            ch_client = ch_client_factory() if ch_client_factory else backup_ch_client()
        except Exception:
            logger.exception("backup: clickhouse client unavailable (run_id=%s)", run_id)

    stores: list[StoreResult] = []
    with tempfile.TemporaryDirectory(prefix="precis-backup-") as tmp:
        workdir = Path(tmp)
        stores.append(
            _backup_postgres(cfg, dest, run_id, workdir, pg_dump_runner or _run_pg_dump)
        )
        if ch_client is not None:
            stores.append(_backup_clickhouse(cfg, dest, run_id, ch_client))
        else:
            stores.append(
                StoreResult(
                    store="clickhouse",
                    outcome="skipped" if cfg.scope.get("clickhouse") == "external" else "failed",
                    detail=(
                        "scope: external — customer-managed store; back it up with your own tooling"
                        if cfg.scope.get("clickhouse") == "external"
                        else "clickhouse client could not be created"
                    ),
                )
            )
        stores.append(_backup_instance(dest, run_id, workdir, instance_dir))
        stores.append(_backup_files(cfg, dest, run_id, workdir))

        attempted = [s for s in stores if s.outcome != "skipped"]
        failed = [s for s in attempted if s.outcome == "failed"]
        outcome = (
            "success" if not failed else "failed" if len(failed) == len(attempted) else "partial"
        )

        manifest = BackupManifest(
            run_id=run_id,
            created_at=started.isoformat(),
            mode=cfg.mode,
            trigger=trigger,
            package_version=package_version(),
            instance_git_sha=read_instance_git_sha(instance_dir),
            scopes=dict(cfg.scope),
            artifacts=[
                ArtifactEntry(
                    store=s.store, key=s.key, sha256=s.sha256,
                    size_bytes=s.size_bytes, outcome=s.outcome, detail=s.detail,
                )
                for s in stores
            ],
            row_counts=_collect_row_counts(cfg, ch_client),
            outcome=outcome,
        )
        mkey = manifest_key(run_id)
        dest.put_bytes(manifest.to_json().encode("utf-8"), mkey)

    finished = datetime.now(timezone.utc)
    result = BackupResult(
        run_id=run_id, mode=cfg.mode, trigger=trigger, outcome=outcome,
        stores=stores, manifest_key=mkey, started_at=started, finished_at=finished,
    )

    try:
        prune_bundles(cfg, dest, protect_run_id=run_id)
    except Exception:
        logger.warning("backup: prune failed (run_id=%s)", run_id, exc_info=True)

    record_history(
        run_id=run_id,
        kind="backup",
        mode=cfg.mode,
        triggered_by=trigger,
        started_at=started,
        finished_at=finished,
        outcome=outcome,
        artifacts={
            s.store: {
                "key": s.key, "sha256": s.sha256, "size_bytes": s.size_bytes,
                "outcome": s.outcome, "detail": s.detail,
            }
            for s in stores
        },
        manifest_key=mkey,
        total_bytes=sum(s.size_bytes for s in stores),
        error_message="; ".join(
            f"{s.store}: {s.detail}" for s in stores if s.outcome == "failed"
        ) or None,
    )

    if outcome != "success":
        logger.error(
            "backup: run %s outcome=%s failed_stores=%s", run_id, outcome, result.failed_stores
        )
        if cfg.alert_webhook_url:
            post_failure(
                cfg.alert_webhook_url,
                {
                    "run_id": run_id,
                    "outcome": outcome,
                    "failed_stores": result.failed_stores,
                    "error": "; ".join(
                        f"{s.store}: {s.detail}" for s in stores if s.outcome == "failed"
                    ),
                },
            )
    else:
        logger.info("backup: run %s complete (total_bytes=%d)", run_id,
                    sum(s.size_bytes for s in stores))
    return result
