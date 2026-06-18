# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Best-effort writes to the ``backup_history`` table.

Best-effort is structural, not defensive: Postgres is one of the stores being
backed up, so the history write must never decide a run's outcome. A failed
insert degrades to a log warning.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def record_history(
    *,
    run_id: str,
    kind: str,
    mode: str,
    triggered_by: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: str,
    artifacts: dict,
    manifest_key: str | None = None,
    total_bytes: int | None = None,
    error_message: str | None = None,
) -> bool:
    try:
        from precis_mcp import db

        db.execute_platform(
            """
            INSERT INTO backup_history (
                run_id, kind, mode, triggered_by, started_at, finished_at,
                duration_ms, outcome, artifacts, manifest_key, total_bytes,
                error_message
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                kind,
                mode,
                triggered_by,
                started_at,
                finished_at,
                int((finished_at - started_at).total_seconds() * 1000),
                outcome,
                json.dumps(artifacts),
                manifest_key,
                total_bytes,
                error_message,
            ),
        )
        return True
    except Exception:
        logger.warning(
            "backup_history write failed (run_id=%s kind=%s) — backup outcome unaffected",
            run_id,
            kind,
            exc_info=True,
        )
        return False
