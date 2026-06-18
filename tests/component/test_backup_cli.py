# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Component tests for the admin CLI `backup` group — dispatch, exit-code
mapping, and the `backup init` render + live-check path."""
from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from precis_mcp import admin_cli
from precis_mcp.backup import BACKUP_EXIT_CODE, BackupConfigError, BundleNotFoundError
from precis_mcp.backup.destination import LocalDestination
from precis_mcp.backup.manifest import ArtifactEntry, BackupManifest, manifest_key
from precis_mcp.backup.runner import BackupResult, StoreResult

LOCAL_DUMP = """
mode: dump
destination:
  type: local
  path: {dest}
schedule:
  cron: "30 2 * * *"
retention:
  postgres: {{ keep: 7 }}
scope:
  files: external
"""


def _write_config(tmp_path, body: str | None = None):
    path = tmp_path / "backup.yml"
    body = body or LOCAL_DUMP.format(dest=tmp_path / "dest")
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_validate_ok(tmp_path, capsys):
    config = _write_config(tmp_path)
    admin_cli.main(["backup", "validate", "--config", str(config)])
    assert "backup config OK" in capsys.readouterr().out


def test_validate_pitr_exits_config_error(tmp_path, capsys):
    config = _write_config(
        tmp_path, LOCAL_DUMP.format(dest=tmp_path / "dest").replace("mode: dump", "mode: pitr")
    )
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["backup", "validate", "--config", str(config)])
    assert exc.value.code == BACKUP_EXIT_CODE[BackupConfigError]
    assert "not yet supported" in capsys.readouterr().err


def test_restore_unknown_bundle_exits_not_found(tmp_path, capsys):
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        admin_cli.main([
            "backup", "restore", "--config", str(config),
            "--id", "20990101T000000Z", "--stores", "postgres",
        ])
    assert exc.value.code == BACKUP_EXIT_CODE[BundleNotFoundError]


def test_init_renders_xml_and_warns_on_missing_disk(tmp_path, ch_client, capsys):
    config = _write_config(tmp_path)
    out = tmp_path / "rendered" / "precis_backup_disk.xml"
    with patch("precis_mcp.db.get_clickhouse_client", return_value=ch_client):
        admin_cli.main([
            "backup", "init", "--config", str(config), "--out", str(out),
        ])
    captured = capsys.readouterr()
    assert out.is_file()
    # Local-destination render embeds no secret and is read by the clickhouse
    # service's uid across the mount — world-readable; 0600 is s3-only.
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert "<type>local</type>" in out.read_text()
    # The fake returns no system.disks row → actionable warning.
    assert "does not see the 'precis_backups' disk" in captured.err
    assert "restart the clickhouse service" in captured.err


def test_init_no_clickhouse_check_passes_clean(tmp_path, capsys):
    config = _write_config(tmp_path)
    out = tmp_path / "rendered.xml"
    admin_cli.main([
        "backup", "init", "--config", str(config), "--out", str(out),
        "--no-clickhouse-check",
    ])
    assert "all checks passed" in capsys.readouterr().out


def test_list_prints_bundles(tmp_path, capsys):
    config = _write_config(tmp_path)
    dest = LocalDestination(tmp_path / "dest")
    manifest = BackupManifest(
        run_id="20260610T023000Z", created_at="2026-06-10T02:30:00+00:00",
        mode="dump", trigger="cli", package_version="t", instance_git_sha=None,
        scopes={},
        artifacts=[ArtifactEntry(store="postgres", key="pg/x.dump", sha256="aa",
                                 size_bytes=10, outcome="success")],
        outcome="success",
    )
    dest.put_bytes(manifest.to_json().encode(), manifest_key(manifest.run_id))

    admin_cli.main(["backup", "list", "--config", str(config)])
    out = capsys.readouterr().out
    assert "20260610T023000Z" in out
    assert "postgres=success" in out


def test_run_dispatch_and_failure_exit_code(tmp_path, capsys):
    config = _write_config(tmp_path)
    ok = BackupResult(
        run_id="r1", mode="dump", trigger="cli", outcome="success",
        stores=[StoreResult(store="postgres", key="pg/r1.dump", size_bytes=5)],
        manifest_key="manifest/r1.json",
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
    )
    with patch("precis_mcp.backup.ops.op_run", return_value=ok) as op_run:
        admin_cli.main(["backup", "run", "--config", str(config)])
    assert op_run.call_args.kwargs["trigger"] == "cli"
    assert "backup r1: success" in capsys.readouterr().out

    failed = BackupResult(
        run_id="r2", mode="dump", trigger="cli", outcome="partial",
        stores=[StoreResult(store="postgres", outcome="failed", detail="boom")],
    )
    with patch("precis_mcp.backup.ops.op_run", return_value=failed):
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["backup", "run", "--config", str(config)])
    assert exc.value.code == 6
