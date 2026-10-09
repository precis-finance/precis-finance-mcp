# SPDX-License-Identifier: Elastic-2.0
"""Transactional artifact registry. Commercial context lives in file_context."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
import tempfile

from precis_mcp.artifacts.ids import generate_file_id, is_valid_file_id
from precis_mcp.auth import get_auth_context, load_permissions
from precis_mcp.datasets import check_access, permission_stamp
from precis_mcp.db import execute_platform, query_platform, transaction_platform

logger = logging.getLogger(__name__)
_MAX_QUOTA_BYTES = 100 * 1024 * 1024
_MAX_FILE_BYTES = 10 * 1024 * 1024
_ALLOWED_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv", ".pdf", ".json", ".yaml", ".yml", ".txt", ".png", ".jpg", ".jpeg", ".py"})


class FileNotFoundError(Exception):
    pass


class QuotaExceeded(Exception):
    pass


class FileTooLarge(Exception):
    pass


class InvalidBasename(ValueError):
    pass


class ConflictError(Exception):
    pass


def _validate_basename(name: str) -> None:
    if (not isinstance(name, str) or not name or len(name) > 255
        or any(c in name for c in ("/", "\\", "\x00", "\r", "\n"))
        or ".." in name or Path(name).suffix.lower() not in _ALLOWED_EXTENSIONS):
        raise InvalidBasename("Invalid or unsupported filename")


def validate_mime_type(value: str) -> str:
    if not isinstance(value, str) or len(value) > 127 or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value):
        raise ValueError("Invalid MIME type")
    return value.lower()


def _user_root(user_id: str) -> Path:
    if not user_id or user_id in {".", ".."} or any(c in user_id for c in ("/", "\\", "\x00")):
        raise ValueError("Invalid artifact owner")
    root = Path(os.getenv("USER_DATA_DIR", "/data/users")).resolve()
    path = (root / user_id).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid artifact owner")
    return path


def _blobs_dir(user_id: str) -> Path:
    return _user_root(user_id) / "blobs"


def _blob_path(user_id: str, file_id: str) -> Path:
    if not is_valid_file_id(file_id):
        raise ValueError("Invalid file_id")
    return _blobs_dir(user_id) / file_id


def _row_to_dict(row: dict) -> dict:
    """Return service metadata; wire descriptors serialize timestamps explicitly."""
    out = dict(row)
    out["size_bytes"] = int(out["size_bytes"])
    return out


def _lock_user(cur, user_id: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (user_id,))


def quota_used(user_id: str, cur=None, *, exclude_upload: str | None = None) -> int:
    """Count stored bytes (including expired blobs) and live upload reservations."""
    # An optional NULL upload ID needs an explicit type for PostgreSQL's IS NULL.
    sql = """SELECT
      COALESCE((SELECT SUM(size_bytes) FROM files WHERE user_id = %s), 0)
      + COALESCE((SELECT SUM(size_bytes) FROM artifact_blob_gc WHERE user_id = %s), 0)
      + COALESCE((SELECT SUM(size_bytes) FROM artifact_uploads
          WHERE user_id = %s AND state <> 'complete'
            AND (%s::text IS NULL OR upload_id <> %s::text)), 0) AS used"""
    params = (user_id, user_id, user_id, exclude_upload, exclude_upload)
    if cur is not None:
        cur.execute(sql, params)
        row = cur.fetchone()
    else:
        rows = query_platform(sql, params)
        row = rows[0] if rows else None
    return int((row or {}).get("used", 0))


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_file(*, user_id: str, basename: str, blob_bytes: bytes, source: str,
                producing_tool: str | None = None, expires_at: datetime | None = None,
                mime_type: str | None = None, provenance: dict | None = None,
                access: dict | None = None, upload_id: str | None = None) -> dict:
    _validate_basename(basename)
    mime_type = validate_mime_type(mime_type or mimetypes.guess_type(basename)[0] or "application/octet-stream")
    if source not in {"user_upload", "agent_tool", "sandbox"}:
        raise ValueError("Invalid file source")
    if len(blob_bytes) > _MAX_FILE_BYTES:
        raise FileTooLarge("File exceeds 10 MiB limit")
    if source != "user_upload" and access is None:
        auth = get_auth_context()
        if auth.user_id != user_id:
            raise ValueError("Artifact owner does not match caller")
        access = {"owner": user_id, "grants": permission_stamp(auth.permissions)}
    file_id = generate_file_id()
    path = _blob_path(user_id, file_id)
    written = False
    try:
        with transaction_platform() as cur:
            _lock_user(cur, user_id)
            if upload_id:
                cur.execute("SELECT * FROM artifact_uploads WHERE upload_id = %s AND user_id = %s AND expires_at > now() FOR UPDATE", (upload_id, user_id))
                upload = cur.fetchone()
                if not upload:
                    raise FileNotFoundError("Upload unavailable")
                if upload["state"] == "complete":
                    cur.execute("SELECT * FROM files WHERE file_id = %s AND user_id = %s AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > now())", (upload["file_id"], user_id))
                    existing = cur.fetchone()
                    if not existing:
                        raise FileNotFoundError("Uploaded artifact unavailable")
                    return _row_to_dict(existing)
                if upload["state"] != "uploaded" or upload["size_bytes"] != len(blob_bytes):
                    raise ValueError("Upload is incomplete")
            if quota_used(user_id, cur, exclude_upload=upload_id) + len(blob_bytes) > _MAX_QUOTA_BYTES:
                raise QuotaExceeded("Storage quota exceeded (100 MiB)")
            _write_atomic(path, blob_bytes)
            written = True
            cur.execute("""INSERT INTO files
              (file_id,user_id,basename,mime_type,size_bytes,sha256,storage_path,
               expires_at,source,producing_tool,provenance,access)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING *""",
              (file_id, user_id, basename, mime_type,
               len(blob_bytes), hashlib.sha256(blob_bytes).hexdigest(), str(path), expires_at,
               source, producing_tool, json.dumps(provenance or {}), json.dumps(access or {})))
            row = cur.fetchone()
            if upload_id:
                cur.execute("UPDATE artifact_uploads SET state = 'complete', file_id = %s WHERE upload_id = %s", (file_id, upload_id))
        return _row_to_dict(row)
    except Exception:
        if written:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("artifact create cleanup failed")
        raise


def authorize_file(row: dict, permissions=None) -> None:
    access = row.get("access") or {}
    if not access:
        return
    if permissions is None:
        try:
            auth = get_auth_context()
            if auth.user_id != row["user_id"]:
                raise FileNotFoundError("Artifact unavailable")
            permissions = auth.permissions
        except RuntimeError:
            permissions = load_permissions(row["user_id"])
    if not check_access(access, row["user_id"], permissions):
        raise FileNotFoundError("Artifact unavailable; source permissions changed")


def get_file(file_id: str, *, user_id: str) -> dict | None:
    if not is_valid_file_id(file_id):
        return None
    rows = query_platform("SELECT * FROM files WHERE file_id = %s AND user_id = %s AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > now())", (file_id, user_id))
    if not rows:
        return None
    try:
        authorize_file(rows[0])
    except FileNotFoundError:
        return None
    return _row_to_dict(rows[0])


def resolve_path(file_id: str, *, user_id: str) -> Path | None:
    row = get_file(file_id, user_id=user_id)
    return Path(row["storage_path"]) if row else None


def update_file_blob(file_id: str, *, user_id: str, blob_bytes: bytes,
                     producing_tool: str | None = None, expected_sha256: str | None = None) -> dict:
    if not is_valid_file_id(file_id):
        raise FileNotFoundError("Artifact unavailable")
    if len(blob_bytes) > _MAX_FILE_BYTES:
        raise FileTooLarge("File exceeds 10 MiB limit")
    path = None
    old_path = None
    written = False
    try:
        with transaction_platform() as cur:
            _lock_user(cur, user_id)
            cur.execute("SELECT * FROM files WHERE file_id = %s AND user_id = %s AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE", (file_id, user_id))
            row = cur.fetchone()
            if not row:
                raise FileNotFoundError("Artifact unavailable")
            authorize_file(row)
            auth = get_auth_context()
            if auth.user_id != user_id:
                raise FileNotFoundError("Artifact unavailable")
            # A tool can add finance data to an originally uploaded workbook.
            # Every rewritten version therefore binds the caller's read grants.
            access = {"owner": user_id, "grants": permission_stamp(auth.permissions)}
            if expected_sha256 is not None and row["sha256"] != expected_sha256:
                raise ConflictError("Artifact changed; reread before updating")
            if quota_used(user_id, cur) - row["size_bytes"] + len(blob_bytes) > _MAX_QUOTA_BYTES:
                raise QuotaExceeded("Storage quota exceeded (100 MiB)")
            old_path = Path(row["storage_path"])
            # Immutable versions make filesystem and SQL commit failures safe:
            # readers retain the old bytes until the new metadata commits.
            path = old_path.parent / f"{file_id}.{row['version'] + 1}.{secrets.token_hex(8)}"
            _write_atomic(path, blob_bytes)
            written = True
            cur.execute("""UPDATE files SET size_bytes = %s, sha256 = %s,
              updated_at = now(), version = version + 1,
              producing_tool = COALESCE(%s, producing_tool), storage_path = %s,
              access = %s::jsonb
              WHERE file_id = %s RETURNING *""",
              (len(blob_bytes), hashlib.sha256(blob_bytes).hexdigest(), producing_tool, str(path), json.dumps(access), file_id))
            updated = cur.fetchone()
            cur.execute("INSERT INTO artifact_blob_gc(storage_path,user_id,size_bytes) VALUES (%s,%s,%s)",
                        (str(old_path), user_id, row["size_bytes"]))
        # Atomic replacement needs at most one file of temporary headroom.
        # Failed retirement remains quota-accounted and blocks further growth.
        written = False
        _collect_retired(user_id=user_id)
        return _row_to_dict(updated)
    except Exception:
        if written and path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("artifact version cleanup retained for reconciliation")
        raise


@contextmanager
def materialize_blob(file_id: str, *, user_id: str):
    row = get_file(file_id, user_id=user_id)
    if row is None:
        raise FileNotFoundError("Artifact unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / row["basename"]
        path.write_bytes(Path(row["storage_path"]).read_bytes())
        yield path, row["basename"], row


@contextmanager
def materialize_for_update(file_id: str, *, user_id: str, producing_tool: str | None = None):
    row = get_file(file_id, user_id=user_id)
    if row is None:
        raise FileNotFoundError("Artifact unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / row["basename"]
        path.write_bytes(Path(row["storage_path"]).read_bytes())
        yield path, row["basename"], row
        update_file_blob(file_id, user_id=user_id, blob_bytes=path.read_bytes(),
                         producing_tool=producing_tool, expected_sha256=row["sha256"])


def list_files(*, user_id: str, include_expired: bool = False) -> list[dict]:
    sql = "SELECT * FROM files WHERE user_id = %s AND deleted_at IS NULL"
    if not include_expired:
        sql += " AND (expires_at IS NULL OR expires_at > now())"
    rows = query_platform(sql + " ORDER BY created_at DESC", (user_id,))
    visible = []
    for row in rows:
        try:
            authorize_file(row)
            visible.append(_row_to_dict(row))
        except FileNotFoundError:
            continue
    return visible


def soft_delete(file_id: str, *, user_id: str) -> bool:
    return execute_platform("UPDATE files SET deleted_at = now() WHERE file_id = %s AND user_id = %s AND deleted_at IS NULL RETURNING file_id", (file_id, user_id)) is not None


def gc_expired() -> dict:
    """Claim each row under a transaction; retain tombstones after unlink failure."""
    removed = 0
    _collect_retired()
    expired = query_platform("SELECT file_id,user_id FROM files WHERE deleted_at IS NOT NULL OR expires_at <= now()")
    for candidate in expired:
        with transaction_platform() as cur:
            _lock_user(cur, candidate["user_id"])
            cur.execute("SELECT * FROM files WHERE file_id = %s AND (deleted_at IS NOT NULL OR expires_at <= now()) FOR UPDATE", (candidate["file_id"],))
            row = cur.fetchone()
            if not row:
                continue
            try:
                Path(row["storage_path"]).unlink(missing_ok=True)
                Path(row["storage_path"]).with_suffix(".receiving").unlink(missing_ok=True)
            except OSError:
                logger.exception("artifact deletion retained for retry")
                cur.execute("UPDATE files SET deleted_at = COALESCE(deleted_at, now()) WHERE file_id = %s", (row["file_id"],))
                continue
            cur.execute("DELETE FROM files WHERE file_id = %s", (row["file_id"],))
            removed += 1
    sessions = query_platform("SELECT upload_id,user_id FROM artifact_uploads WHERE expires_at <= now()")
    for session in sessions:
        with transaction_platform() as cur:
            _lock_user(cur, session["user_id"])
            cur.execute("SELECT * FROM artifact_uploads WHERE upload_id = %s AND expires_at <= now() FOR UPDATE", (session["upload_id"],))
            row = cur.fetchone()
            if not row:
                continue
            try:
                Path(row["storage_path"]).unlink(missing_ok=True)
                Path(row["storage_path"]).with_suffix(".receiving").unlink(missing_ok=True)
            except OSError:
                logger.exception("upload staging deletion retained for retry")
                continue
            cur.execute("DELETE FROM artifact_uploads WHERE upload_id = %s", (session["upload_id"],))
    reconcile_orphans()
    return {"soft_deleted": len(expired), "blobs_removed": removed}


def reconcile_orphans() -> None:
    """Retry cleanup after process/commit failures without racing recent writes."""
    rows = query_platform("SELECT storage_path FROM files UNION ALL SELECT storage_path FROM artifact_uploads UNION ALL SELECT storage_path FROM artifact_blob_gc")
    live = {str(Path(row["storage_path"]).resolve()) for row in rows}
    live.update(str(Path(row["storage_path"]).with_suffix(".receiving").resolve()) for row in rows)
    root = Path(os.getenv("USER_DATA_DIR", "/data/users"))
    threshold = time.time() - 3600
    for directory in (*root.glob("*/blobs"), *root.glob("*/artifact_staging")):
        if directory.is_symlink():
            continue
        for path in directory.iterdir():
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < threshold and str(path.resolve()) not in live:
                    path.unlink()
            except OSError:
                logger.exception("artifact orphan cleanup will retry")


def _collect_retired(*, user_id: str | None = None) -> None:
    sql = "SELECT * FROM artifact_blob_gc"
    try:
        rows = query_platform(sql + (" WHERE user_id = %s" if user_id is not None else ""), (user_id,) if user_id is not None else ())
    except Exception:
        logger.exception("retired artifact lookup will retry")
        return
    for candidate in rows:
        try:
            with transaction_platform() as cur:
                _lock_user(cur, candidate["user_id"])
                cur.execute("SELECT * FROM artifact_blob_gc WHERE storage_path = %s FOR UPDATE", (candidate["storage_path"],))
                row = cur.fetchone()
                if not row:
                    continue
                Path(row["storage_path"]).unlink(missing_ok=True)
                cur.execute("DELETE FROM artifact_blob_gc WHERE storage_path = %s", (row["storage_path"],))
        except Exception:
            logger.exception("retired artifact blob retained for retry")
