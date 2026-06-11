# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Restore a backup bundle — real restore with safety guards, or a drill.

Guards: a real restore refuses a non-empty target without ``--force``, and
artifact checksums are verified against the manifest *before* any store is
touched. The drill never touches live targets: Postgres restores into
``precis_platform_drill``, ClickHouse into ``*_drill`` databases, and success
is measured against the manifest's row-count baseline. The instance artifact
is always extracted beside the live mount, never over it — the instance
directory is a read-only git checkout owned by the operator.

Known v1 drill limitation: views inside ``semantic_drill`` still reference
``live.*``; the drill verifies tables by row count and views by existence
only. The full run_metric-equivalence drill is a named follow-on gap in
docs/architecture/11-backup-and-dr.md.
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

from precis_mcp.backup import (
    BackupExecutionError,
    BundleNotFoundError,
    RestoreGuardError,
)
from precis_mcp.backup.ch_render import BACKUP_DISK_NAME
from precis_mcp.backup.config import BackupConfig
from precis_mcp.backup.destination import BackupDestination, build_destination
from precis_mcp.backup.history import record_history
from precis_mcp.backup.manifest import BackupManifest, manifest_key, sha256_file
from precis_mcp.backup.runner import CH_DATABASES, backup_ch_client

logger = logging.getLogger(__name__)

DRILL_PG_DB = "precis_platform_drill"
DRILL_SUFFIX = "_drill"

# Stores this module can restore; CH restores server-side from its disk.
_FETCHED_STORES = ("postgres", "instance", "files")


@dataclass
class StoreRestoreResult:
    store: str
    outcome: str  # 'success' | 'failed' | 'skipped'
    detail: str | None = None


@dataclass
class VerificationItem:
    name: str
    expected: int
    actual: int

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


@dataclass
class RestoreResult:
    run_id: str
    drill: bool
    outcome: str
    stores: list[StoreRestoreResult] = field(default_factory=list)
    verification: list[VerificationItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Postgres helpers — module-level so tests can monkeypatch them.
# ---------------------------------------------------------------------------


def _pg_connect_kwargs(dbname: str) -> dict:
    return dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        dbname=dbname,
        connect_timeout=int(os.getenv("PG_CONNECT_TIMEOUT", "5")),
    )


def _pg_execute_admin(sql: str) -> None:
    """Run a statement that needs autocommit (CREATE/DROP DATABASE) against
    the maintenance database."""
    import psycopg

    with psycopg.connect(**_pg_connect_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(sql)  # type: ignore[arg-type,call-overload]


def _pg_database_exists(dbname: str) -> bool:
    import psycopg

    with psycopg.connect(**_pg_connect_kwargs("postgres")) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        return row is not None


def _pg_table_count(dbname: str) -> int:
    import psycopg

    with psycopg.connect(**_pg_connect_kwargs(dbname)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchone()
        return int(row[0]) if row else 0


def _pg_table_counts(dbname: str) -> dict[str, int]:
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(**_pg_connect_kwargs(dbname)) as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
        for (name,) in tables:
            row = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()  # type: ignore[arg-type,call-overload]
            counts[name] = int(row[0]) if row else 0
    return counts


def _run_pg_restore(dump_path: Path, dbname: str) -> None:
    cmd = [
        "pg_restore",
        "--format=custom",
        "--clean", "--if-exists",
        "--no-owner",
        "-h", os.getenv("PGHOST", "localhost"),
        "-p", os.getenv("PGPORT", "5432"),
        "-U", os.getenv("PGUSER", "postgres"),
        "-d", dbname,
        str(dump_path),
    ]
    proc = subprocess.run(
        cmd, env=os.environ.copy(), capture_output=True, text=True, timeout=3600
    )
    if proc.returncode != 0:
        raise BackupExecutionError(f"pg_restore failed: {proc.stderr.strip()[-500:]}")


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------


def _ch_nonempty_databases(ch_client) -> list[str]:
    dbs = ", ".join(f"'{db}'" for db in CH_DATABASES)
    result = ch_client.query(
        "SELECT DISTINCT database FROM system.tables "
        f"WHERE database IN ({dbs}) AND total_rows > 0"
    )
    return [row[0] for row in result.result_rows]


def _ch_drill_table_counts(ch_client) -> dict[str, int]:
    dbs = ", ".join(f"'{db}{DRILL_SUFFIX}'" for db in CH_DATABASES)
    result = ch_client.query(
        "SELECT database, name, total_rows FROM system.tables "
        f"WHERE database IN ({dbs}) AND engine NOT LIKE '%View%'"
    )
    return {
        f"{database}.{name}": int(total_rows or 0)
        for database, name, total_rows in result.result_rows
    }


# ---------------------------------------------------------------------------
# Tar extraction — py3.12 data filter prevents path traversal
# ---------------------------------------------------------------------------


def _extract_tar(tarball: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(out_dir, filter="data")


# ---------------------------------------------------------------------------
# Bundle restore
# ---------------------------------------------------------------------------


def _fetch_and_verify(
    dest: BackupDestination, manifest: BackupManifest, selected: set[str], workdir: Path
) -> dict[str, Path]:
    """Fetch the process-pushed artifacts and verify checksums before any
    store is touched. Returns store -> local path."""
    local: dict[str, Path] = {}
    for store in _FETCHED_STORES:
        if store not in selected:
            continue
        artifact = manifest.artifact(store)
        if artifact is None or not artifact.key or artifact.outcome != "success":
            continue
        path = workdir / Path(artifact.key).name
        dest.fetch_to(artifact.key, path)
        if artifact.sha256 and sha256_file(path) != artifact.sha256:
            raise BackupExecutionError(
                f"checksum mismatch for {artifact.key} — bundle is corrupt or "
                "tampered; refusing to restore"
            )
        local[store] = path
    return local


def restore_bundle(
    cfg: BackupConfig,
    run_id: str,
    *,
    drill: bool = False,
    force: bool = False,
    stores: set[str] | None = None,
    target_db: str | None = None,
    keep_drill: bool = False,
    instance_out_dir: Path | None = None,
    trigger: str = "cli",
    ch_client_factory: Callable[[], object] | None = None,
    dest_factory: Callable[[], BackupDestination] | None = None,
    pg_restore_runner: Callable[[Path, str], None] | None = None,
) -> RestoreResult:
    started = datetime.now(timezone.utc)
    dest = dest_factory() if dest_factory else build_destination(cfg, credential="reader")
    pg_restore = pg_restore_runner or _run_pg_restore

    try:
        manifest = BackupManifest.from_json(dest.get_bytes(manifest_key(run_id)))
    except FileNotFoundError as exc:
        raise BundleNotFoundError(
            f"no manifest for run {run_id!r} at the destination — "
            "use `backup list` for available bundles"
        ) from exc

    available = {
        a.store for a in manifest.artifacts if a.outcome == "success" and a.key
    }
    selected = (stores or available) & available

    ch_client = None
    if "clickhouse" in selected:
        ch_client = ch_client_factory() if ch_client_factory else backup_ch_client()

    results: list[StoreRestoreResult] = []
    verification: list[VerificationItem] = []

    with tempfile.TemporaryDirectory(prefix="precis-restore-") as tmp:
        workdir = Path(tmp)
        local = _fetch_and_verify(dest, manifest, selected, workdir)

        if drill:
            results, verification = _run_drill(
                manifest, selected, local, ch_client, pg_restore, keep_drill
            )
        else:
            results = _run_real_restore(
                selected, local, ch_client, pg_restore,
                force=force, target_db=target_db,
                instance_out_dir=instance_out_dir, run_id=run_id,
            )

    failed = [r for r in results if r.outcome == "failed"]
    verify_failed = [v for v in verification if not v.ok]
    outcome = "failed" if failed or verify_failed else "success"

    record_history(
        run_id=run_id,
        kind="restore_drill" if drill else "restore",
        mode=cfg.mode,
        triggered_by=trigger,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        outcome=outcome,
        artifacts={
            **{r.store: {"outcome": r.outcome, "detail": r.detail} for r in results},
            **(
                {
                    "verification": {
                        "checked": len(verification),
                        "mismatches": [
                            {"name": v.name, "expected": v.expected, "actual": v.actual}
                            for v in verify_failed
                        ],
                    }
                }
                if drill
                else {}
            ),
        },
        manifest_key=manifest_key(run_id),
        error_message="; ".join(
            [f"{r.store}: {r.detail}" for r in failed]
            + [f"{v.name}: expected {v.expected}, got {v.actual}" for v in verify_failed]
        ) or None,
    )
    return RestoreResult(
        run_id=run_id, drill=drill, outcome=outcome,
        stores=results, verification=verification,
    )


def _run_real_restore(
    selected: set[str],
    local: dict[str, Path],
    ch_client,
    pg_restore: Callable[[Path, str], None],
    *,
    force: bool,
    target_db: str | None,
    instance_out_dir: Path | None,
    run_id: str,
) -> list[StoreRestoreResult]:
    results: list[StoreRestoreResult] = []

    if "postgres" in selected and "postgres" in local:
        target = target_db or os.getenv("PLATFORM_DB_NAME", "precis_platform")
        if _pg_database_exists(target) and _pg_table_count(target) > 0 and not force:
            raise RestoreGuardError(
                f"target database {target!r} is non-empty — pass --force to overwrite"
            )
        if not _pg_database_exists(target):
            _pg_execute_admin(f'CREATE DATABASE "{target}"')
        pg_restore(local["postgres"], target)
        results.append(StoreRestoreResult(store="postgres", outcome="success",
                                          detail=f"restored into {target!r}"))

    if "clickhouse" in selected and ch_client is not None:
        nonempty = _ch_nonempty_databases(ch_client)
        if nonempty and not force:
            raise RestoreGuardError(
                f"ClickHouse database(s) {nonempty} are non-empty — "
                "pass --force to drop and overwrite"
            )
        for db in CH_DATABASES:
            ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")
        databases = ", ".join(f"DATABASE {db}" for db in CH_DATABASES)
        ch_client.command(
            f"RESTORE {databases} FROM Disk('{BACKUP_DISK_NAME}', '{run_id}.zip')"
        )
        results.append(StoreRestoreResult(store="clickhouse", outcome="success"))

    if "instance" in selected and "instance" in local:
        out = instance_out_dir or Path(tempfile.mkdtemp(prefix=f"instance-{run_id}-"))
        _extract_tar(local["instance"], out)
        results.append(StoreRestoreResult(
            store="instance", outcome="success",
            detail=f"extracted to {out} — review and swap into the instance "
                   "mount yourself; the live checkout is never overwritten",
        ))

    if "files" in selected and "files" in local:
        user_data_dir = Path(os.getenv("USER_DATA_DIR", "/data/users"))
        if force:
            _extract_tar(local["files"], user_data_dir)
            results.append(StoreRestoreResult(store="files", outcome="success",
                                              detail=f"restored into {user_data_dir}"))
        else:
            out = Path(tempfile.mkdtemp(prefix=f"files-{run_id}-"))
            _extract_tar(local["files"], out)
            results.append(StoreRestoreResult(
                store="files", outcome="success",
                detail=f"extracted to {out} (pass --force to restore into "
                       f"{user_data_dir} directly)",
            ))
    return results


def _run_drill(
    manifest: BackupManifest,
    selected: set[str],
    local: dict[str, Path],
    ch_client,
    pg_restore: Callable[[Path, str], None],
    keep_drill: bool,
) -> tuple[list[StoreRestoreResult], list[VerificationItem]]:
    results: list[StoreRestoreResult] = []
    verification: list[VerificationItem] = []

    if "postgres" in selected and "postgres" in local:
        try:
            # Drill DBs are tool-owned; a stale one from an aborted drill is dropped.
            _pg_execute_admin(f'DROP DATABASE IF EXISTS "{DRILL_PG_DB}"')
            _pg_execute_admin(f'CREATE DATABASE "{DRILL_PG_DB}"')
            pg_restore(local["postgres"], DRILL_PG_DB)
            counts = _pg_table_counts(DRILL_PG_DB)
            for key, expected in manifest.row_counts.items():
                if key.startswith("pg."):
                    table = key[3:]
                    verification.append(VerificationItem(
                        name=key, expected=expected, actual=counts.get(table, 0)
                    ))
            results.append(StoreRestoreResult(store="postgres", outcome="success",
                                              detail=f"drilled into {DRILL_PG_DB!r}"))
        except Exception as exc:
            logger.exception("restore drill: postgres failed")
            results.append(StoreRestoreResult(store="postgres", outcome="failed",
                                              detail=str(exc)))
        finally:
            if not keep_drill:
                try:
                    _pg_execute_admin(f'DROP DATABASE IF EXISTS "{DRILL_PG_DB}"')
                except Exception:
                    logger.warning("restore drill: could not drop %s", DRILL_PG_DB)

    if "clickhouse" in selected and ch_client is not None:
        try:
            for db in CH_DATABASES:
                ch_client.command(f"DROP DATABASE IF EXISTS {db}{DRILL_SUFFIX} SYNC")
            renames = ", ".join(
                f"DATABASE {db} AS {db}{DRILL_SUFFIX}" for db in CH_DATABASES
            )
            ch_client.command(
                f"RESTORE {renames} FROM Disk('{BACKUP_DISK_NAME}', '{manifest.run_id}.zip')"
            )
            counts = _ch_drill_table_counts(ch_client)
            for key, expected in manifest.row_counts.items():
                if key.startswith("ch."):
                    _, db, table = key.split(".", 2)
                    verification.append(VerificationItem(
                        name=key, expected=expected,
                        actual=counts.get(f"{db}{DRILL_SUFFIX}.{table}", 0),
                    ))
            results.append(StoreRestoreResult(store="clickhouse", outcome="success"))
        except Exception as exc:
            logger.exception("restore drill: clickhouse failed")
            results.append(StoreRestoreResult(store="clickhouse", outcome="failed",
                                              detail=str(exc)))
        finally:
            if not keep_drill:
                for db in CH_DATABASES:
                    try:
                        ch_client.command(f"DROP DATABASE IF EXISTS {db}{DRILL_SUFFIX} SYNC")
                    except Exception:
                        logger.warning("restore drill: could not drop %s%s", db, DRILL_SUFFIX)

    return results, verification
