# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Long-running watcher daemon — `python -m precis_mcp.ingestion.watcher_daemon`.

Wires the watcher's dependencies from the environment, instantiates `Watcher`,
and runs `tick()` in a loop until the process is signalled. Suitable for
invocation from a systemd unit, a Kubernetes Deployment, or a `docker compose`
service.

The watcher is idempotent across replicas — per-binding processed-keys are
read from `load_history` every tick, so two watchers running simultaneously
race only on which fires first; the orchestrator's per-binding advisory lock
prevents concurrent runs of the same `(binding, period)`. The daemon
therefore does **not** acquire a daemon-level lock.

Environment:
    PRECIS_INTEGRATIONS_ROOT       — registry YAML root (defaults to in-repo)
    PRECIS_WATCHER_INTERVAL_SECONDS — tick interval (default: 30)
    PRECIS_S3_*                    — S3 store config (optional)
    PRECIS_SFTP_*                  — SFTP store config (optional)
    PRECIS_INGEST_UPLOAD_DIR       — local-FS store for HTTPS uploads
                                     (required if http_upload bindings exist)
    CH*, PG*                       — shared spine connectivity (the platform
                                     Postgres also backs the ingestion lock)
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from precis_mcp.ingestion.registry import IntegrationRegistry
from precis_mcp.ingestion.watcher import Watcher
from precis_mcp.ingestion.wiring import (
    _default_integrations_root,
    build_default_context,
    build_object_stores_from_env,
    build_store_factory,
)
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.watcher_daemon")


# ---------------------------------------------------------------------------
# Signal handling — clean shutdown on SIGTERM / SIGINT
# ---------------------------------------------------------------------------


class _StopFlag:
    """Mutable flag the run-loop polls between ticks."""

    def __init__(self) -> None:
        self.stop = False

    def __call__(self, signum, frame) -> None:  # noqa: D401
        _logger.info(
            "ingest.watcher_daemon.shutdown_requested",
            signal=signum,
        )
        self.stop = True


def _install_signal_handlers() -> _StopFlag:
    flag = _StopFlag()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, flag)
        except ValueError:
            # Not running on the main thread; skip — tests exercise the loop
            # via `stop_after` instead.
            pass
    return flag


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _interval_seconds() -> float:
    return float(os.getenv("PRECIS_WATCHER_INTERVAL_SECONDS", "30"))


def main(*, integrations_root: Optional[Path] = None) -> int:
    """Daemon entry point. Returns the process exit code."""
    root = integrations_root or _default_integrations_root()
    _logger.info("ingest.watcher_daemon.start", integrations_root=str(root))

    registry = IntegrationRegistry.load(root)

    # Object stores — only the ones configured via env vars get built.
    # Required by the watcher itself (it lists files in object stores);
    # the orchestrator no longer needs them (Ibis-DuckDB reads files
    # directly from absolute paths).
    stores = build_object_stores_from_env()
    if not stores:
        _logger.warning(
            "ingest.watcher_daemon.no_stores",
            hint=(
                "No PRECIS_S3_*, PRECIS_SFTP_*, or PRECIS_INGEST_UPLOAD_DIR "
                "set; watch-mode bindings will fail per-tick. Configure at "
                "least one transport."
            ),
        )

    ctx = build_default_context(registry)
    store_factory = build_store_factory(stores)
    watcher = Watcher(ctx, store_factory=store_factory)

    interval = _interval_seconds()
    stop = _install_signal_handlers()
    _logger.info(
        "ingest.watcher_daemon.loop_start",
        interval_seconds=interval,
        bindings=len(registry.bindings),
    )

    while not stop.stop:
        try:
            watcher.tick()
        except Exception:
            _logger.exception("ingest.watcher_daemon.tick_failed")
        # Sleep in small increments so SIGTERM lands promptly between ticks.
        slept = 0.0
        while slept < interval and not stop.stop:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0

    _logger.info("ingest.watcher_daemon.exit", reason="signal")
    return 0


if __name__ == "__main__":  # pragma: no cover — entry point
    sys.exit(main())
