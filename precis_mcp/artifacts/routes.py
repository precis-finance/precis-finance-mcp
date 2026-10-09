# SPDX-License-Identifier: Elastic-2.0
"""Shared HTTPS artifact transfer, independent of the commercial middleware."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from precis_mcp.artifacts import registry, tokens, uploads
from precis_mcp.artifacts.policy import external_uploads_enabled
from precis_mcp.auth import AuthContext, UserPermissions, clear_auth_context, load_permissions, set_auth_context
from precis_mcp.db import execute_platform

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])
_transfers = asyncio.Semaphore(4)
_user_transfers: dict[str, tuple[asyncio.Semaphore, int]] = {}
logger = logging.getLogger(__name__)


@asynccontextmanager
async def transfer_slot(user_id: str):
    # Count waiting requests as well as active ones; remove idle owners to keep
    # this process-local limiter bounded. Cross-worker capacity is SQL quota.
    semaphore, count = _user_transfers.get(user_id, (asyncio.Semaphore(2), 0))
    if semaphore.locked() or _transfers.locked():
        raise HTTPException(429, "Artifact service busy; retry shortly")
    _user_transfers[user_id] = (semaphore, count + 1)
    try:
        async with semaphore, _transfers:
            yield
    finally:
        _, count = _user_transfers[user_id]
        if count == 1:
            del _user_transfers[user_id]
        else:
            _user_transfers[user_id] = (semaphore, count - 1)


class RedactTransferTokens:
    """Remove URL credentials from the ASGI scope used by server access logs."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in {"/api/artifacts/download", "/api/artifacts/upload"}:
            params = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
            scope["artifact_token"] = params.get("t", [""])[0]
            scope["query_string"] = b""
        await self.app(scope, receive, send)


def live_permissions(user_id: str):
    if os.getenv("PRECIS_AUTH_MODE") == "devkey" and os.getenv("ENABLE_MCP_DEV_SERVER") == "1" and user_id == "dev":
        return UserPermissions(user_id="dev", is_admin=True)
    return load_permissions(user_id)


def _claims(request: Request, purpose: str):
    try:
        claims = tokens.verify_token(purpose, request.scope.get("artifact_token", ""))
        permissions = live_permissions(claims["sub"])
        return claims, permissions
    except Exception:
        raise HTTPException(404, "Artifact transfer unavailable") from None


def _download_bytes(claims, permissions):
    set_auth_context(AuthContext(user_id=claims["sub"], permissions=permissions))
    try:
        row = registry.get_file(claims["object_id"], user_id=claims["sub"])
        if row is None or row["version"] != claims.get("version") or row["sha256"] != claims.get("sha256"):
            raise HTTPException(404, "Artifact transfer unavailable")
        # Capture bounded bytes rather than leaving a mutable path for
        # FileResponse to open later. A concurrent replacement cannot change
        # the bytes a version-bound capability receives.
        with Path(row["storage_path"]).open("rb") as stream:
            data = stream.read(registry._MAX_FILE_BYTES + 1)
        if len(data) > registry._MAX_FILE_BYTES or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), row["sha256"]):
            raise HTTPException(404, "Artifact transfer unavailable")
        return row, data
    finally:
        clear_auth_context()


@router.get("/download")
async def download(request: Request):
    claims, permissions = await asyncio.to_thread(_claims, request, "download")
    async with transfer_slot(claims["sub"]):
        try:
            row, data = await asyncio.to_thread(_download_bytes, claims, permissions)
        except (registry.FileNotFoundError, OSError):
            raise HTTPException(404, "Artifact transfer unavailable") from None
    from urllib.parse import quote
    return Response(data, media_type=row["mime_type"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['basename'], safe='')}",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.put("/upload")
async def upload(request: Request):
    # Also reject capabilities minted before an operator disabled uploads.
    # The commercial multipart /api/files/upload route is independent.
    if not external_uploads_enabled():
        raise HTTPException(404, "Artifact upload unavailable")
    claims, _ = await asyncio.to_thread(_claims, request, "upload")
    async with transfer_slot(claims["sub"]):
        row = await asyncio.to_thread(execute_platform,
          "UPDATE artifact_uploads SET state = 'receiving' WHERE upload_id = %s AND user_id = %s AND state = 'pending' AND expires_at > now() RETURNING *",
          (claims["object_id"], claims["sub"]))
        if row is None:
            raise HTTPException(409, "Upload unavailable or already receiving bytes")
        path = Path(row["storage_path"])
        temporary = path.with_suffix(".receiving")
        total = 0
        finalizing = False
        try:
            expected = row["size_bytes"]
            if expected != claims.get("size_bytes") or expected > registry._MAX_FILE_BYTES:
                raise HTTPException(400, "Upload size mismatch")
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdecimal() or int(declared) != expected):
                raise HTTPException(400, "Upload size mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            timeout = min(60, max(0, (row["expires_at"] - datetime.now(timezone.utc)).total_seconds()))
            async with asyncio.timeout(timeout):
                with temporary.open("wb") as stream:
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > expected:
                            raise HTTPException(413, "Upload exceeds reserved size")
                        await asyncio.to_thread(stream.write, chunk)
                    if total != expected:
                        raise HTTPException(400, "Upload size mismatch")
                    await asyncio.to_thread(stream.flush)
                    await asyncio.to_thread(os.fsync, stream.fileno())
            os.replace(temporary, path)
            finalizing = True
            changed = await asyncio.to_thread(execute_platform,
              "UPDATE artifact_uploads SET state = 'uploaded' WHERE upload_id = %s AND user_id = %s AND state = 'receiving' AND expires_at > now() RETURNING upload_id",
              (row["upload_id"], claims["sub"]))
            if changed is None:
                raise HTTPException(410, "Upload reservation expired")
            return {"upload_id": row["upload_id"], "state": "uploaded", "size_bytes": total}
        except BaseException as exc:
            if finalizing:
                # A cancelled thread may still commit uploaded state. Do not
                # delete its bytes while the SQL outcome is uncertain; complete
                # can consume them, or expiry GC reclaims the reservation.
                raise
            for candidate in (temporary, path):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    logger.exception("upload cleanup retained for retry")
            # Cancellation also releases the receiving state. The reservation
            # itself remains bounded by expiry and is cleaned by the GC worker.
            await asyncio.shield(asyncio.to_thread(execute_platform,
              "UPDATE artifact_uploads SET state = 'pending' WHERE upload_id = %s AND user_id = %s AND state = 'receiving'",
              (row["upload_id"], claims["sub"])))
            if isinstance(exc, TimeoutError):
                raise HTTPException(408, "Upload timed out; retry within the reservation") from None
            raise
