# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Core database-fake contracts used by the published MCP tests."""
from copy import deepcopy

import pytest

from tests.fakes.fake_platform_db import FakePlatformDB


def test_core_fake_exposes_only_its_supported_tables_and_user_fields():
    db = FakePlatformDB()
    assert {name for name, value in vars(db).items() if isinstance(value, list)} == {
        "users", "profiles", "user_profile_assignments", "profile_audit",
        "validation_rules", "files", "artifact_uploads", "artifact_blob_gc",
        "security_audit_log", "backup_history",
    }
    assert set(db._default_user("reader")) == {
        "id", "is_admin", "is_disabled", "created_at", "updated_at",
    }


def test_core_user_and_profile_queries_share_the_same_state():
    db = FakePlatformDB()
    db.execute("INSERT INTO users (id, is_admin, external_id) VALUES (%s, %s, %s)",
               ("reader", False, "external-reader"))
    db.execute("INSERT INTO profiles VALUES (%s, %s, %s, %s, %s)",
               ("finance", "Finance", "", '{"scenarios":{"*":{"analyst":{}}}}', "admin"))
    db.execute("INSERT INTO user_profile_assignments VALUES (%s, %s, %s, %s)",
               ("reader", "finance", "admin", "admin_cli"))
    definitions = db.query(
        "SELECT p.definition FROM user_profile_assignments upa JOIN profiles p "
        "ON p.profile_id = upa.profile_id WHERE upa.user_id = %s", ("reader",))
    assert definitions == [{"definition": {"scenarios": {"*": {"analyst": {}}}}}]
    db.execute("UPDATE users SET is_disabled = %s WHERE id = %s", (True, "reader"))
    row = db.query("SELECT * FROM users WHERE id = %s", ("reader",))[0]
    assert row["external_id"] == "external-reader"
    assert row["is_disabled"] is True


def test_artifact_quota_includes_reservations_and_retired_blobs():
    db = FakePlatformDB()
    db.files.append({"user_id": "reader", "size_bytes": 10})
    db.artifact_uploads.append({
        "upload_id": "reservation", "user_id": "reader", "size_bytes": 20,
        "state": "pending",
    })
    db.artifact_blob_gc.append({"user_id": "reader", "size_bytes": 5})
    sql = "SELECT SUM(size_bytes) AS used FROM files WHERE user_id = %s"
    assert db.query(sql, ("reader",)) == [{"used": 35}]
    assert db.query(sql, ("reader", None, None, "reservation")) == [{"used": 15}]


@pytest.mark.parametrize("failure_point", ["body", "commit"])
def test_artifact_transaction_restores_files_and_related_accounting(failure_point):
    db = FakePlatformDB()
    db.files.append({"file_id": "file-one", "user_id": "reader", "size_bytes": 10})
    db.artifact_uploads.append({"upload_id": "upload-one", "file_id": "file-one"})
    expected = deepcopy((db.files, db.artifact_uploads, db.artifact_blob_gc))
    failure = RuntimeError("transaction failed")
    if failure_point == "commit":
        db._commit_should_fail = failure
    with pytest.raises(RuntimeError, match="transaction failed"):
        with db.transaction() as cursor:
            cursor.execute("DELETE FROM files WHERE file_id = %s", ("file-one",))
            cursor.execute("INSERT INTO artifact_blob_gc VALUES (%s, %s, %s)",
                           ("retired-path", "reader", 10))
            if failure_point == "body":
                raise failure
    assert (db.files, db.artifact_uploads, db.artifact_blob_gc) == expected
    assert db._commit_should_fail is None


def test_artifact_transaction_commits_successful_changes():
    db = FakePlatformDB()
    db.files.append({"file_id": "file-one"})
    db.artifact_uploads.append({"upload_id": "upload-one", "file_id": "file-one"})
    with db.transaction() as cursor:
        cursor.execute("DELETE FROM files WHERE file_id = %s", ("file-one",))
    assert db.files == []
    assert db.artifact_uploads[0]["file_id"] is None
