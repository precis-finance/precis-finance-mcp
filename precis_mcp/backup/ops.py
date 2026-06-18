# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Backup operations core — the surface-agnostic logic layer.

Shared by the admin CLI and the sidecar scheduler, mirroring the
`precis_mcp.admin_ops` split: this module raises the domain exceptions from
`precis_mcp.backup`, never prints or exits — the CLI maps them to exit codes.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from precis_mcp.backup.ch_render import BACKUP_DISK_NAME, render_clickhouse_disk_xml
from precis_mcp.backup.config import (
    BackupConfig,
    ResolvedCreds,
    load_backup_config,
    resolve_credentials,
)
from precis_mcp.backup.destination import build_destination
from precis_mcp.backup.manifest import BackupManifest
from precis_mcp.backup.restore import RestoreResult, restore_bundle
from precis_mcp.backup.runner import BackupResult, run_backup

logger = logging.getLogger(__name__)

_PROBE_KEY = "manifest/.write_probe"


@dataclass
class InitReport:
    xml_path: Path
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def default_render_path() -> Path:
    """`<deployment root>/deploy/secrets/precis_backup_disk.xml` — the path
    the compose file mounts into the clickhouse service. Same resolution rule
    as `default_instance_dir`: source-checkout sibling of the package, else
    the working directory (the installed console script inside the image)."""
    from precis_mcp.clickhouse_init import PROJECT_ROOT

    root = PROJECT_ROOT if (PROJECT_ROOT / "deploy").is_dir() else Path.cwd()
    return root / "deploy" / "secrets" / "precis_backup_disk.xml"


def op_validate(config_path: Path | None = None) -> BackupConfig:
    """Parse + static checks only; no store or destination is contacted."""
    return load_backup_config(config_path)


def op_init(
    config_path: Path | None = None,
    *,
    render_to: Path | None = None,
    check_clickhouse: bool = True,
) -> InitReport:
    cfg = load_backup_config(config_path)
    report = InitReport(xml_path=render_to or default_render_path())
    report.notices.extend(cfg.notices)

    creds: ResolvedCreds | None = None
    if cfg.destination.type == "s3":
        creds = resolve_credentials(cfg.credentials.writer or "")

    xml = render_clickhouse_disk_xml(cfg, creds)
    report.xml_path.parent.mkdir(parents=True, exist_ok=True)
    report.xml_path.write_text(xml, encoding="utf-8")
    # The rendered XML is mounted into the clickhouse service, which runs as
    # a different uid than this writer — 0600 would make the mount unreadable
    # and crash-loop the server. Restrict only when the XML actually embeds a
    # secret (s3 writer credential); the local-destination render is plain
    # disk config. For s3, ownership must then be transferred to the
    # clickhouse uid (see the backups guide).
    os.chmod(report.xml_path, 0o600 if creds is not None else 0o644)

    try:
        dest = build_destination(cfg, credential="writer")
        dest.put_bytes(b"", _PROBE_KEY)
        dest.delete(_PROBE_KEY)
    except Exception as exc:
        report.warnings.append(f"destination write probe failed: {exc}")

    if check_clickhouse and cfg.scope.get("clickhouse") == "managed":
        try:
            from precis_mcp.db import get_clickhouse_client

            client = get_clickhouse_client()
            result = client.query(
                "SELECT name FROM system.disks WHERE name = %(name)s",
                parameters={"name": BACKUP_DISK_NAME},
            )
            if not result.result_rows:
                report.warnings.append(
                    f"ClickHouse does not see the {BACKUP_DISK_NAME!r} disk yet — "
                    "mount the rendered XML at /etc/clickhouse-server/config.d/"
                    "precis_backup_disk.xml (set PRECIS_BACKUP_CH_CONFIG) and "
                    "restart the clickhouse service"
                )
        except Exception as exc:
            report.warnings.append(f"ClickHouse disk check failed: {exc}")

    if cfg.expect_worm:
        verified = False
        if cfg.destination.type == "s3":
            try:
                import boto3  # type: ignore[import-not-found]

                rcreds = resolve_credentials(
                    cfg.credentials.reader or cfg.credentials.writer or ""
                )
                client = boto3.client(
                    "s3",
                    region_name=cfg.destination.region,
                    endpoint_url=cfg.destination.endpoint,
                    aws_access_key_id=rcreds.access_key_id,
                    aws_secret_access_key=rcreds.secret_access_key,
                )
                lock = client.get_object_lock_configuration(Bucket=cfg.destination.bucket)
                verified = bool(lock.get("ObjectLockConfiguration"))
            except Exception:
                verified = False
        if not verified:
            report.warnings.append(
                "expect_worm is set but object-lock could not be verified on the "
                "destination — confirm the bucket's WORM/lifecycle policy manually"
            )
    return report


def op_run(config_path: Path | None = None, *, trigger: str = "cli") -> BackupResult:
    cfg = load_backup_config(config_path)
    return run_backup(cfg, trigger=trigger)


def op_list(config_path: Path | None = None) -> list[dict]:
    cfg = load_backup_config(config_path)
    dest = build_destination(cfg, credential="reader")
    out: list[dict] = []
    for key in dest.list_keys("manifest/"):
        if key.endswith(".write_probe"):
            continue
        try:
            manifest = BackupManifest.from_json(dest.get_bytes(key))
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.warning("backup list: unreadable manifest %s", key)
            continue
        out.append(
            {
                "run_id": manifest.run_id,
                "created_at": manifest.created_at,
                "mode": manifest.mode,
                "trigger": manifest.trigger,
                "outcome": manifest.outcome,
                "total_bytes": sum(a.size_bytes for a in manifest.artifacts),
                "stores": {a.store: a.outcome for a in manifest.artifacts},
                "instance_git_sha": manifest.instance_git_sha,
            }
        )
    return sorted(out, key=lambda m: m["run_id"], reverse=True)


def op_restore(
    config_path: Path | None = None,
    *,
    run_id: str,
    drill: bool = False,
    force: bool = False,
    stores: set[str] | None = None,
    target_db: str | None = None,
    keep_drill: bool = False,
) -> RestoreResult:
    cfg = load_backup_config(config_path)
    return restore_bundle(
        cfg,
        run_id,
        drill=drill,
        force=force,
        stores=stores,
        target_db=target_db,
        keep_drill=keep_drill,
    )
