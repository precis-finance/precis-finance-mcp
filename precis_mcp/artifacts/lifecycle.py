# SPDX-License-Identifier: Elastic-2.0
"""Common artifact startup/readiness and independent periodic cleanup."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import tempfile
from precis_mcp.artifacts import registry, tokens
from precis_mcp.db import get_redis_client, query_platform

logger = logging.getLogger(__name__)


def check_artifacts() -> None:
    tokens.require_configuration()
    query_platform("SELECT file_id,version,provenance,access FROM files LIMIT 0")
    query_platform("SELECT upload_id,state FROM artifact_uploads LIMIT 0")
    query_platform("SELECT storage_path,size_bytes FROM artifact_blob_gc LIMIT 0")
    root = Path(os.getenv("USER_DATA_DIR", "/data/users"))
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=root):
        pass


async def _cleanup() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(registry.gc_expired)
        except Exception:
            logger.exception("artifact cleanup will retry")


async def start_artifacts():
    await asyncio.to_thread(check_artifacts)
    await asyncio.to_thread(get_redis_client().ping)
    return asyncio.create_task(_cleanup(), name="artifact-cleanup")


async def stop_artifacts(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def readiness_response():
    """Probe the common dependencies without exposing connection details."""
    from fastapi.responses import JSONResponse
    from precis_mcp import db
    from precis_mcp.analytical import selected_backend
    from precis_mcp.analytical.config import DatabricksConfig

    checks = {}
    backend = selected_backend()
    analytical_probe = (("clickhouse", lambda: db.query_clickhouse("SELECT 1"))
                        if backend == "clickhouse" else ("databricks_config", DatabricksConfig.from_env))
    for name, probe in (
        ("postgres", lambda: db.query_platform("SELECT 1")),
        analytical_probe,
        ("redis", lambda: db.get_redis_client().ping()),
        ("artifacts", check_artifacts),
    ):
        try:
            probe()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {type(exc).__name__}"
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
