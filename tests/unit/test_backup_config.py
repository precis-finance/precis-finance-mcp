# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for precis_mcp/backup/config.py — backup.yml parse + validation."""
from __future__ import annotations

import textwrap

import pytest

from precis_mcp.backup import BackupConfigError
from precis_mcp.backup.config import load_backup_config

LOCAL_DUMP = """
mode: dump
destination:
  type: local
  path: /backups
schedule:
  cron: "30 2 * * *"
retention:
  postgres:   { keep: 7 }
  clickhouse: { keep: 7 }
  instance:   { keep: 7 }
scope:
  postgres: managed
  clickhouse: managed
  files: external
expect_worm: false
"""

S3_DUMP = """
mode: dump
destination:
  type: s3
  endpoint: https://s3.eu-central-1.amazonaws.com
  bucket: acme-precis-backups
  prefix: prod
  region: eu-central-1
credentials:
  writer: BACKUP_WRITER
  reader: BACKUP_READER
schedule:
  cron: "30 2 * * *"
retention:
  postgres:   { keep: 14, days: 90 }
  clickhouse: { keep: 14, days: 90 }
  instance:   { keep: 30 }
  files:      { keep: 7 }
scope:
  postgres: managed
  clickhouse: managed
  files: external
encryption:
  kms_key_id: null
expect_worm: true
alert:
  webhook_url: https://hooks.example.com/precis-backup
"""


def _write(tmp_path, body: str):
    path = tmp_path / "backup.yml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_local_dump_example_parses(tmp_path):
    cfg = load_backup_config(_write(tmp_path, LOCAL_DUMP))
    assert cfg.mode == "dump"
    assert cfg.destination.type == "local"
    assert cfg.destination.path == "/backups"
    assert cfg.schedule_cron == "30 2 * * *"
    assert cfg.retention["postgres"].keep == 7
    assert cfg.scope == {"postgres": "managed", "clickhouse": "managed", "files": "external"}
    assert cfg.expect_worm is False
    assert cfg.alert_webhook_url is None


def test_s3_dump_example_parses(tmp_path):
    cfg = load_backup_config(_write(tmp_path, S3_DUMP))
    assert cfg.destination.type == "s3"
    assert cfg.destination.bucket == "acme-precis-backups"
    assert cfg.destination.prefix == "prod"
    assert cfg.credentials.writer == "BACKUP_WRITER"
    assert cfg.credentials.reader == "BACKUP_READER"
    assert cfg.retention["postgres"].days == 90
    assert cfg.expect_worm is True
    assert cfg.alert_webhook_url == "https://hooks.example.com/precis-backup"
    assert cfg.kms_key_id is None


def test_committed_instance_example_parses():
    from precis_mcp.clickhouse_init import default_instance_dir

    cfg = load_backup_config(default_instance_dir() / "backup.yml")
    assert cfg.mode == "dump"
    assert cfg.destination.type == "local"


def test_pitr_mode_rejected_with_clear_message(tmp_path):
    body = LOCAL_DUMP.replace("mode: dump", "mode: pitr")
    with pytest.raises(BackupConfigError, match="not yet supported"):
        load_backup_config(_write(tmp_path, body))


def test_incremental_rejected_in_dump_mode(tmp_path):
    body = LOCAL_DUMP.replace(
        'cron: "30 2 * * *"', 'cron: "30 2 * * *"\n  incremental: "0 * * * *"'
    )
    with pytest.raises(BackupConfigError, match="incremental"):
        load_backup_config(_write(tmp_path, body))


def test_wal_window_rejected_in_dump_mode(tmp_path):
    body = LOCAL_DUMP.replace("retention:", "retention:\n  wal_window_days: 14")
    with pytest.raises(BackupConfigError, match="wal_window_days"):
        load_backup_config(_write(tmp_path, body))


def test_s3_without_writer_credential_rejected(tmp_path):
    body = S3_DUMP.replace("credentials:\n  writer: BACKUP_WRITER\n  reader: BACKUP_READER\n", "")
    with pytest.raises(BackupConfigError, match="credentials.writer"):
        load_backup_config(_write(tmp_path, body))


def test_bad_cron_rejected(tmp_path):
    body = LOCAL_DUMP.replace("30 2 * * *", "99 99 * *")
    with pytest.raises(BackupConfigError, match="cron"):
        load_backup_config(_write(tmp_path, body))


def test_inline_secret_value_rejected(tmp_path):
    body = LOCAL_DUMP.replace(
        "destination:", "destination:\n  secret_access_key: AKIA-oops"
    )
    with pytest.raises(BackupConfigError, match="credential references"):
        load_backup_config(_write(tmp_path, body))


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(BackupConfigError, match="unknown key"):
        load_backup_config(_write(tmp_path, LOCAL_DUMP + "\nfrequency: daily\n"))


def test_local_without_path_rejected(tmp_path):
    body = LOCAL_DUMP.replace("  path: /backups\n", "")
    with pytest.raises(BackupConfigError, match="destination.path"):
        load_backup_config(_write(tmp_path, body))


def test_s3_without_endpoint_or_region_rejected(tmp_path):
    body = S3_DUMP.replace("  endpoint: https://s3.eu-central-1.amazonaws.com\n", "")
    body = body.replace("  region: eu-central-1\n", "")
    with pytest.raises(BackupConfigError, match="endpoint .*or a region|endpoint"):
        load_backup_config(_write(tmp_path, body))


def test_scope_defaults_files_external(tmp_path):
    body = LOCAL_DUMP.replace(
        "scope:\n  postgres: managed\n  clickhouse: managed\n  files: external\n", ""
    )
    cfg = load_backup_config(_write(tmp_path, body))
    assert cfg.scope["files"] == "external"
    assert cfg.scope["postgres"] == "managed"


def test_retention_requires_keep_or_days(tmp_path):
    body = LOCAL_DUMP.replace("postgres:   { keep: 7 }", "postgres:   {}")
    with pytest.raises(BackupConfigError, match="keep, days, or both"):
        load_backup_config(_write(tmp_path, body))


def test_local_with_credentials_noticed_not_rejected(tmp_path):
    body = LOCAL_DUMP.replace(
        "destination:", "credentials:\n  writer: BACKUP_WRITER\ndestination:"
    )
    cfg = load_backup_config(_write(tmp_path, body))
    assert any("ignored" in n for n in cfg.notices)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(BackupConfigError, match="not found"):
        load_backup_config(tmp_path / "absent.yml")
