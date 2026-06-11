# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Backup and restore for the Précis stores.

One declarative config (`instance/backup.yml`) drives every mechanism: the
ClickHouse backup-disk XML drop-in is rendered from it, the per-store runners
read it, and the sidecar scheduler fires on its cron. Bundle layout at the
destination: ``ch/``, ``pg/``, ``instance/``, ``files/``, ``manifest/`` —
manifest written last, so manifest presence means the bundle is complete.

Domain exceptions are surface-agnostic, mirroring `precis_mcp.admin_ops`:
the CLI maps them to exit codes.
"""
from __future__ import annotations


class BackupError(Exception):
    """Base for backup/restore failures."""


class BackupConfigError(BackupError):
    """backup.yml failed to parse or validate (exit 2)."""


class BundleNotFoundError(BackupError):
    """No manifest for the requested run id at the destination (exit 4)."""


class RestoreGuardError(BackupError):
    """Restore refused: target is non-empty and --force not given (exit 5)."""


class BackupExecutionError(BackupError):
    """A backup or restore operation failed against a real store (exit 6)."""


BACKUP_EXIT_CODE: dict[type[BackupError], int] = {
    BackupConfigError: 2,
    BundleNotFoundError: 4,
    RestoreGuardError: 5,
    BackupExecutionError: 6,
}
