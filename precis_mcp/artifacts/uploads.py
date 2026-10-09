# SPDX-License-Identifier: Elastic-2.0
"""Upload reservations and idempotent registration of bounded staged files."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import builtins
import secrets
from precis_mcp.artifacts import registry, tokens
from precis_mcp.db import execute_platform, query_platform, transaction_platform


def begin_upload(user_id: str, filename: str, mime_type: str, size_bytes: int) -> dict:
    tokens.require_configuration()
    registry._validate_basename(filename)
    mime_type = registry.validate_mime_type(mime_type)
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0 or size_bytes > registry._MAX_FILE_BYTES:
        raise registry.FileTooLarge("Uploads must contain 1 byte to 10 MiB")
    upload_id = "upload_" + secrets.token_hex(16)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.DEFAULT_TTL_SECONDS)
    path = registry._user_root(user_id) / "artifact_staging" / upload_id
    with transaction_platform() as cur:
        registry._lock_user(cur, user_id)
        if registry.quota_used(user_id, cur) + size_bytes > registry._MAX_QUOTA_BYTES:
            raise registry.QuotaExceeded("Storage quota exceeded (100 MiB)")
        cur.execute("""INSERT INTO artifact_uploads(upload_id,user_id,basename,mime_type,size_bytes,storage_path,state,expires_at)
          VALUES (%s,%s,%s,%s,%s,%s,'pending',%s)""", (upload_id,user_id,filename,mime_type,size_bytes,str(path),expiry))
    return {"upload_id": upload_id, "upload_url": tokens.transfer_url("upload",user_id,upload_id,size_bytes=size_bytes),
            "method": "PUT", "expires_at": expiry.isoformat(), "size_bytes": size_bytes}


def get_upload(upload_id: str, user_id: str) -> dict:
    rows = query_platform("SELECT * FROM artifact_uploads WHERE upload_id = %s AND user_id = %s AND expires_at > now()", (upload_id,user_id))
    if not rows:
        raise registry.FileNotFoundError("Upload unavailable")
    return rows[0]


def complete_upload(upload_id: str, user_id: str) -> dict:
    upload = get_upload(upload_id,user_id)
    if upload["state"] == "complete":
        row = registry.get_file(upload["file_id"],user_id=user_id)
        if row is None:
            raise registry.FileNotFoundError("Uploaded artifact unavailable")
        return row
    if upload["state"] != "uploaded":
        raise ValueError("Upload bytes have not been received")
    path = Path(upload["storage_path"])
    try:
        with path.open("rb") as stream:
            blob = stream.read(registry._MAX_FILE_BYTES + 1)
    except builtins.FileNotFoundError:
        latest = get_upload(upload_id, user_id)
        if latest["state"] == "complete":
            row = registry.get_file(latest["file_id"], user_id=user_id)
            if row is not None:
                return row
        raise registry.FileNotFoundError("Upload bytes unavailable") from None
    if len(blob) != upload["size_bytes"]:
        raise ValueError("Upload size mismatch")
    row = registry.create_file(user_id=user_id, basename=upload["basename"],blob_bytes=blob,
                              source="user_upload",mime_type=upload["mime_type"],upload_id=upload_id,
                              expires_at=datetime.now(timezone.utc)+timedelta(hours=24))
    # Completion is committed before deleting staging; cleanup can retry unlink.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return row
