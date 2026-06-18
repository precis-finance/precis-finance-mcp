# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Long-running scheduler daemon — `python -m precis_mcp.ingestion.scheduler_daemon`.

Builds the OrchestratorContext from the environment, instantiates the
`Scheduler`, and runs `tick()` in a loop until the process is signalled.

Stateless across restarts — the scheduler queries `load_history` on every
tick to compute the next fire time. Two replicas can run safely; the
orchestrator's per-binding advisory lock prevents concurrent runs of the same
(binding, period).

Environment:
    PRECIS_INTEGRATIONS_ROOT          — registry YAML root
    PRECIS_SCHEDULER_INTERVAL_SECONDS — tick interval (default: 60)
    CH*, PG*                          — shared spine connectivity (the platform
                                        Postgres also backs the ingestion lock)
    <SECRET_REF_UPPER>_*              — Ibis backend credentials per source
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.scheduler import Scheduler
from precis_mcp.ingestion.wiring import (
    _default_integrations_root,
    build_default_context,
)
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.scheduler_daemon")


def _interval_seconds() -> float:
    return float(os.getenv("PRECIS_SCHEDULER_INTERVAL_SECONDS", "60"))


class _StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def __call__(self, signum, frame) -> None:  # noqa: D401
        _logger.info("ingest.scheduler_daemon.shutdown_requested", signal=signum)
        self.stop = True


def _install_signal_handlers() -> _StopFlag:
    flag = _StopFlag()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, flag)
        except ValueError:
            pass
    return flag


def main(*, integrations_root: Optional[Path] = None) -> int:
    root = integrations_root or _default_integrations_root()
    _logger.info("ingest.scheduler_daemon.start", integrations_root=str(root))

    registry = IntegrationRegistry.load(root)
    ctx = build_default_context(registry)
    scheduler = Scheduler(ctx)

    interval = _interval_seconds()
    stop = _install_signal_handlers()
    _logger.info(
        "ingest.scheduler_daemon.loop_start",
        interval_seconds=interval,
        bindings=len(registry.bindings),
    )

    while not stop.stop:
        try:
            scheduler.tick()
        except Exception:
            _logger.exception("ingest.scheduler_daemon.tick_failed")
        slept = 0.0
        while slept < interval and not stop.stop:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0

    _logger.info("ingest.scheduler_daemon.exit", reason="signal")
    return 0


if __name__ == "__main__":  # pragma: no cover — entry point
    sys.exit(main())
