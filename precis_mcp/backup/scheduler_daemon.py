# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Backup scheduler sidecar — `python -m precis_mcp.backup.scheduler_daemon`.

Reads the cron from backup.yml and invokes the same backup code path the CLI
uses, in-process. Stateless across restarts: a missed fire is not back-filled
— the next cron occurrence wins (`backup_history` records what actually ran).
A config error exits non-zero so a misconfigured sidecar restart-loops
visibly instead of idling silently.

Environment:
    PG*, CH*                  — shared spine connectivity
    BACKUP_WRITER_* / BACKUP_READER_* — destination credentials (s3)
    PRECIS_BACKUP_CONFIG      — backup.yml path override
    PRECIS_BACKUP_CH_TIMEOUT  — BACKUP/RESTORE command timeout (default 3600 s)
"""
from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import precis_mcp.secrets  # noqa: F401 — resolve *_FILE env vars at import
from precis_mcp.backup import BackupConfigError
from precis_mcp.backup.config import load_backup_config
from precis_mcp.observability import get_logger

_logger = get_logger("backup.scheduler_daemon")


class _StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def __call__(self, signum, frame) -> None:  # noqa: D401
        _logger.info("backup.scheduler_daemon.shutdown_requested", signal=signum)
        self.stop = True


def _install_signal_handlers() -> _StopFlag:
    flag = _StopFlag()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, flag)
        except ValueError:
            pass
    return flag


def _config_path() -> Path | None:
    override = os.getenv("PRECIS_BACKUP_CONFIG")
    return Path(override) if override else None


def main() -> int:
    try:
        cfg = load_backup_config(_config_path())
    except BackupConfigError as exc:
        _logger.error("backup.scheduler_daemon.config_error", error=str(exc))
        return 2

    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(cfg.schedule_cron)
    stop = _install_signal_handlers()
    _logger.info(
        "backup.scheduler_daemon.start",
        cron=cfg.schedule_cron,
        destination=cfg.destination.type,
        mode=cfg.mode,
    )

    previous: datetime | None = None
    while not stop.stop:
        now = datetime.now(timezone.utc)
        next_fire = trigger.get_next_fire_time(previous, now)
        if next_fire is None:  # pragma: no cover — crontab triggers always fire
            _logger.error("backup.scheduler_daemon.no_next_fire")
            return 1
        _logger.info("backup.scheduler_daemon.next_fire", at=next_fire.isoformat())

        while not stop.stop and datetime.now(timezone.utc) < next_fire:
            time.sleep(1.0)
        if stop.stop:
            break

        previous = next_fire
        try:
            from precis_mcp.backup.ops import op_run

            result = op_run(_config_path(), trigger="scheduler")
            log = _logger.info if result.outcome == "success" else _logger.error
            log(
                "backup.scheduler_daemon.run_done",
                run_id=result.run_id,
                outcome=result.outcome,
                failed_stores=result.failed_stores,
            )
        except Exception:
            _logger.exception("backup.scheduler_daemon.run_failed")

    _logger.info("backup.scheduler_daemon.exit", reason="signal")
    return 0


if __name__ == "__main__":  # pragma: no cover — entry point
    sys.exit(main())
