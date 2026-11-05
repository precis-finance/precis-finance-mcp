# SPDX-License-Identifier: Elastic-2.0
"""Artifact authority is limited to user-owned files, never finance writes."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from precis_mcp.artifacts import registry, tokens, uploads
from precis_mcp.datasets import fetch_cached_read_result
from precis_mcp.excel.snapshot_export import write_snapshot_xlsx

ARTIFACT_MUTATING_TOOLS = frozenset({"export_dataset", "begin_file_upload", "complete_file_upload", "import_chatgpt_file"})


def _error(exc):
    if isinstance(exc, registry.QuotaExceeded):
        return {"error": "Artifact quota exceeded", "error_type": "quota"}
    if isinstance(exc, registry.FileTooLarge):
        return {"error": "Artifact exceeds 10 MiB", "error_type": "size"}
    if isinstance(exc, (ValueError, registry.FileNotFoundError)):
        return {"error": str(exc), "error_type": "validation"}
    import logging
    logging.getLogger(__name__).exception("artifact operation failed")
    return {"error": "Artifact service unavailable; retry shortly", "error_type": "unavailable"}


def export_dataset(data_ref: str, format: str, user_id: str) -> dict:
    """Export a previously run report's cached data_ref as JSON or XLSX.

    Use for standard exports of run_statement/run_metric results already shown
    to the user. Never reruns the query. XLSX uses the shared server renderer,
    retaining financial source/region metadata for conversion and consistent
    formatting. Do not rebuild a standard report from JSON in execute_python.
    Returns an artifact descriptor/download link, or an explicit error if the
    snapshot expired or access changed. Fresh queries may return different values.
    """
    try:
        tokens.require_configuration()
        if format not in {"json", "xlsx"}:
            raise ValueError("format must be json or xlsx")
        payload, error = fetch_cached_read_result(user_id, data_ref)
        if error:
            return {"error": error, "error_type": "dataset"}
        source = payload["source"]
        basename = source["tool"] + "_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "." + format
        if format == "json":
            blob = json.dumps(payload["result"], allow_nan=False, ensure_ascii=False).encode()
        else:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / basename
                write_snapshot_xlsx(payload["result"], source, path)
                blob = path.read_bytes()
        row = registry.create_file(user_id=user_id, basename=basename,blob_bytes=blob,source="agent_tool",
                                  producing_tool="export_dataset",expires_at=datetime.now(timezone.utc)+timedelta(hours=24),
                                  provenance={"source": source, "snapshot_created_at": payload["created_at"]},access=payload["access"])
        return {"artifact": tokens.artifact_descriptor(row)}
    except Exception as exc:
        return _error(exc)


def get_file_download(file_id: str, user_id: str) -> dict:
    """Renew a download capability for an available, owned artifact."""
    try:
        row = registry.get_file(file_id, user_id=user_id)
        if row is None:
            raise registry.FileNotFoundError("Artifact unavailable")
        return {"artifact": tokens.artifact_descriptor(row)}
    except Exception as exc:
        return _error(exc)


def begin_file_upload(filename: str, mime_type: str, size_bytes: int, user_id: str) -> dict:
    """Reserve quota and obtain an HTTPS PUT URL; no finance ingestion occurs."""
    try:
        return uploads.begin_upload(user_id,filename,mime_type,size_bytes)
    except Exception as exc:
        return _error(exc)


def complete_file_upload(upload_id: str, user_id: str) -> dict:
    """Register received bytes once; retries return the same file_id."""
    try:
        return {"artifact": tokens.artifact_descriptor(uploads.complete_upload(upload_id,user_id))}
    except Exception as exc:
        return _error(exc)


def register_artifact_tools(reg):
    for tool in (export_dataset,get_file_download,begin_file_upload,complete_file_upload,import_chatgpt_file):
        reg.tool()(tool)


def import_chatgpt_file(file: dict, user_id: str) -> dict:
    """Import a host-provided attachment into the owned artifact store."""
    try:
        tokens.require_configuration()
        from precis_mcp.artifacts.host_import import import_attachment
        return {"artifact": tokens.artifact_descriptor(import_attachment(file,user_id))}
    except Exception as exc:
        return _error(exc)
