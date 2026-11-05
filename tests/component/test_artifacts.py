# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared snapshot export and raw-byte transfer against local shared fakes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from precis_mcp.artifacts import registry, tokens, uploads
from precis_mcp.artifacts.routes import RedactTransferTokens, router
from precis_mcp.auth import AuthContext, UserPermissions, clear_auth_context, set_auth_context
from precis_mcp import datasets
from precis_mcp.tools import artifact_tools
from tests.fakes.fake_redis import FakeRedis


@pytest.fixture
def services(db, tmp_path, monkeypatch):
    monkeypatch.setenv("PRECIS_MCP_UPLOADS_ENABLED", "true")
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_SIGNING_KEY", "test-only-" + "x" * 32)
    monkeypatch.setenv("PRECIS_BASE_URL", "https://precis.example")
    permissions = UserPermissions(user_id="alice", is_admin=True)
    set_auth_context(AuthContext(user_id="alice", permissions=permissions))
    for module in (registry, uploads):
        monkeypatch.setattr(module, "query_platform", db.query)
        monkeypatch.setattr(module, "transaction_platform", db.transaction)
    monkeypatch.setattr(registry, "execute_platform", db.execute)
    monkeypatch.setattr("precis_mcp.artifacts.routes.execute_platform", db.execute)
    monkeypatch.setattr("precis_mcp.artifacts.routes.live_permissions", lambda user: UserPermissions(user_id=user, is_admin=True))
    monkeypatch.setattr(datasets, "get_redis_client", lambda: client_redis)
    client_redis = FakeRedis(decode_responses=True)
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(RedactTransferTokens)
    with TestClient(app) as client:
        yield db, client, permissions
    clear_auth_context()


def local_url(url):
    parsed = urlsplit(url)
    return parsed.path + "?" + parsed.query


@pytest.mark.parametrize("setting", [None, "false", "invalid"])
def test_disabled_external_upload_rejects_existing_capability_but_allows_export(services, monkeypatch, setting):
    db, client, _ = services
    begin = uploads.begin_upload("alice", "budget.json", "application/json", 7)
    if setting is None:
        monkeypatch.delenv("PRECIS_MCP_UPLOADS_ENABLED")
    else:
        monkeypatch.setenv("PRECIS_MCP_UPLOADS_ENABLED", setting)

    assert client.put(local_url(begin["upload_url"]), content=b'{"x":1}').status_code == 404
    assert db.artifact_uploads[0]["state"] == "pending"
    assert not Path(db.artifact_uploads[0]["storage_path"]).exists()
    snapshot = {"columns": ["amount"], "rows": [{"amount": 1.25}]}
    ref = datasets.cache_read_result(snapshot, "inspect_rows", {"limit": 1})
    exported = artifact_tools.export_dataset(ref, "json", "alice")["artifact"]
    renewed = artifact_tools.get_file_download(exported["file_id"], "alice")["artifact"]
    assert json.loads(client.get(local_url(renewed["download_url"])).content) == snapshot


def test_upload_complete_download_and_renew_use_one_identity(services):
    db, client, _ = services
    begin = artifact_tools.begin_file_upload("budget.json", "application/json", 7, "alice")
    received = client.put(local_url(begin["upload_url"]), content=b'{"x":1}')
    assert received.status_code == 200
    completed = artifact_tools.complete_file_upload(begin["upload_id"], "alice")["artifact"]
    retry = artifact_tools.complete_file_upload(begin["upload_id"], "alice")["artifact"]
    renewed = artifact_tools.get_file_download(completed["file_id"], "alice")["artifact"]
    assert retry["file_id"] == renewed["file_id"] == completed["file_id"]
    assert len(db.files) == 1
    assert client.get(local_url(renewed["download_url"])).content == b'{"x":1}'
    assert "error" in artifact_tools.get_file_download(completed["file_id"], "bob")
    assert client.get("/api/artifacts/download").status_code == 404


def test_partial_upload_releases_receiving_state_and_can_retry(services):
    db, client, _ = services
    begin = artifact_tools.begin_file_upload("budget.json", "application/json", 7, "alice")
    url = local_url(begin["upload_url"])
    assert client.put(url, content=b"small").status_code == 400
    assert db.artifact_uploads[0]["state"] == "pending"
    assert client.put(url, content=b'{"x":1}').status_code == 200
    assert client.put(url, content=b'{"x":1}').status_code == 409


def test_download_capability_binds_blob_version_and_live_permissions(services, monkeypatch):
    _, client, _ = services
    row = registry.create_file(user_id="alice", basename="data.json", blob_bytes=b"old", source="agent_tool")
    descriptor = tokens.artifact_descriptor(row)
    assert client.get(local_url(descriptor["download_url"])).content == b"old"
    updated = registry.update_file_blob(row["file_id"], user_id="alice", blob_bytes=b"new")
    assert client.get(local_url(descriptor["download_url"])).status_code == 404
    fresh = tokens.artifact_descriptor(updated)
    assert client.get(local_url(fresh["download_url"])).content == b"new"
    monkeypatch.setattr("precis_mcp.artifacts.routes.live_permissions", lambda user: UserPermissions(user_id=user, is_admin=False))
    assert client.get(local_url(fresh["download_url"])).status_code == 404


def test_expiry_denied_before_gc_and_expired_bytes_count_until_cleanup(services):
    db, client, _ = services
    row = registry.create_file(user_id="alice", basename="data.json", blob_bytes=b"abc", source="user_upload")
    url = tokens.artifact_descriptor(row)["download_url"]
    db.files[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert registry.get_file(row["file_id"], user_id="alice") is None
    assert client.get(local_url(url)).status_code == 404
    assert registry.quota_used("alice") == 3
    registry.gc_expired()
    assert registry.quota_used("alice") == 0


def test_tool_rewriting_an_upload_binds_new_version_to_read_grants(services, monkeypatch):
    _, client, _ = services
    row = registry.create_file(user_id="alice", basename="budget.json", blob_bytes=b"uploaded", source="user_upload")
    updated = registry.update_file_blob(row["file_id"], user_id="alice", blob_bytes=b"finance result", producing_tool="execute_python")
    url = tokens.artifact_descriptor(updated)["download_url"]
    assert client.get(local_url(url)).content == b"finance result"
    monkeypatch.setattr("precis_mcp.artifacts.routes.live_permissions", lambda user: UserPermissions(user_id=user, is_admin=False))
    assert client.get(local_url(url)).status_code == 404


def test_reserved_quota_serializes_concurrent_uploads(services, monkeypatch):
    monkeypatch.setattr(registry, "_MAX_QUOTA_BYTES", 10)
    def reserve(_):
        try:
            return uploads.begin_upload("alice", "budget.json", "application/json", 6)
        except registry.QuotaExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(value is not None for value in results) == 1


def test_snapshot_exports_preserve_rows_precision_and_literal_excel_text(services):
    db, client, _ = services
    snapshot = {"kind": "inspection", "source_key": "budget", "columns": ["name", "amount"], "rows": [{"name": "=1+1", "amount": 1.23456789}]}
    ref = datasets.cache_read_result(snapshot, "inspect_rows", {"limit": 200})
    exported = artifact_tools.export_dataset(ref, "json", "alice")["artifact"]
    assert json.loads(client.get(local_url(exported["download_url"])).content) == snapshot
    excel = artifact_tools.export_dataset(ref, "xlsx", "alice")["artifact"]
    row = registry.get_file(excel["file_id"], user_id="alice")
    wb = load_workbook(BytesIO(Path(row["storage_path"]).read_bytes()))
    assert wb["Inspection"]["A2"].value == "=1+1"
    assert wb["Inspection"]["A2"].data_type == "s"
    assert wb["Inspection"]["B2"].value == 1.23456789
    assert row["provenance"]["source"]["tool"] == "inspect_rows"
    assert len(db.files) == 2


@pytest.mark.parametrize("tool", ["run_metric", "run_statement"])
def test_financial_snapshot_export_has_convertible_report_provenance(services, tool):
    from precis_mcp.excel.meta import read_meta
    _, client, _ = services
    snapshot = {"kind": "metric" if tool == "run_metric" else "statement", "dimensions": [], "scale": 0, "decimals": None,
                "caption": {"description": "Financial report", "period": "2026-01 to 2026-03"},
                "scenarios": [{"alias": "Actuals"}],
                "rows": [{"grain": "detail", "dimensions": {}, "item": {"key": "margin", "label": "Margin", "format": "percent", "decimals": 2},
                          "values": {"Actuals": 8.7654321}},
                         {"grain": "detail", "dimensions": {}, "item": {"key": "total", "label": "Total", "format": "number", "style": "total", "separator_above": True},
                          "values": {"Actuals": 0}}]}
    args = {"metrics": ["margin"]} if tool == "run_metric" else {"statement": "pnl"}
    args.update(period_start="2026-01", period_end="2026-03", scenarios=[{"scenario": "actuals", "alias": "Actuals"}], scale=0, decimals=None)
    ref = datasets.cache_read_result(snapshot, tool, args)
    artifact = artifact_tools.export_dataset(ref, "xlsx", "alice")["artifact"]
    wb = load_workbook(BytesIO(client.get(local_url(artifact["download_url"])).content))
    meta = read_meta(wb)
    sheet, entry = next(iter(meta["sheets"].items()))
    region = entry["regions"][0]
    assert "source" not in entry
    assert region["layout"] == "report"
    assert region["source"] == {"tool": tool, "params": args}
    assert region["origin"] == "https://precis.example"
    assert region["shape"]["column_keys"] == ["label", "Actuals"]
    assert region["shape"]["footer_rows"] == 1
    assert wb[sheet]["A1"].value == "Financial report"
    assert region["shape"]["row_count"] == 2
    assert wb[sheet].max_row == region["shape"]["data_start_row"] + 4
    assert list(wb.defined_names[region["range_name"]].destinations)[0][0] == sheet
    cell = wb[sheet].cell(region["shape"]["data_start_row"] + 2, 2)
    assert cell.value == 8.7654321 and '0.00"%"' in cell.number_format
    assert wb[sheet].cell(cell.row + 1, 1).value == "Total"
    assert wb[sheet].cell(cell.row + 1, 2).value == 0


def test_failed_db_commit_keeps_original_blob_and_metadata(services):
    db, _, _ = services
    row = registry.create_file(user_id="alice", basename="data.json", blob_bytes=b"old", source="user_upload")
    db._commit_should_fail = RuntimeError("commit failed")
    with pytest.raises(RuntimeError):
        registry.update_file_blob(row["file_id"], user_id="alice", blob_bytes=b"new")
    current = registry.get_file(row["file_id"], user_id="alice")
    assert current["sha256"] == row["sha256"]
    assert Path(current["storage_path"]).read_bytes() == b"old"


def test_retired_blob_failure_keeps_quota_and_retries_cleanup(services, monkeypatch):
    db, _, _ = services
    monkeypatch.setattr(registry, "_MAX_QUOTA_BYTES", 10)
    row = registry.create_file(user_id="alice", basename="data.json", blob_bytes=b"old", source="user_upload")
    unlink = Path.unlink
    def fail_old(path, *args, **kwargs):
        if str(path) == row["storage_path"]:
            raise PermissionError("simulated unlink failure")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "unlink", fail_old)
        updated = registry.update_file_blob(row["file_id"], user_id="alice", blob_bytes=b"newer")
        assert registry.quota_used("alice") == 8
        assert Path(updated["storage_path"]).read_bytes() == b"newer"
        with pytest.raises(registry.QuotaExceeded):
            registry.create_file(user_id="alice", basename="next.json", blob_bytes=b"123", source="user_upload")
    registry.gc_expired()
    assert registry.quota_used("alice") == 5
    assert db.artifact_blob_gc == []


def test_concurrent_completion_registers_exactly_one_file(services):
    db, client, _ = services
    begin = uploads.begin_upload("alice", "budget.json", "application/json", 7)
    assert client.put(local_url(begin["upload_url"]), content=b'{"x":1}').status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: uploads.complete_upload(begin["upload_id"], "alice"), range(2)))
    assert rows[0]["file_id"] == rows[1]["file_id"]
    assert len(db.files) == 1
