# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for precis_mcp/backup/ch_render.py — the generated config.d XML."""
from __future__ import annotations

import pytest

from precis_mcp.backup import BackupConfigError
from precis_mcp.backup.config import (
    BackupConfig,
    CredentialRefs,
    DestinationCfg,
    ResolvedCreds,
    RetentionCfg,
)
from precis_mcp.backup.ch_render import render_clickhouse_disk_xml, s3_disk_endpoint


def _cfg(destination: DestinationCfg) -> BackupConfig:
    return BackupConfig(
        mode="dump",
        destination=destination,
        credentials=CredentialRefs(writer="BACKUP_WRITER", reader=None),
        schedule_cron="30 2 * * *",
        retention={"postgres": RetentionCfg(keep=7)},
        scope={"postgres": "managed", "clickhouse": "managed", "files": "external"},
    )


def test_local_disk_xml():
    cfg = _cfg(DestinationCfg(type="local", path="/backups"))
    xml = render_clickhouse_disk_xml(cfg)
    assert "<type>local</type>" in xml
    assert "<path>/backups/ch/</path>" in xml
    assert "<allowed_disk>precis_backups</allowed_disk>" in xml
    assert "GENERATED" in xml


def test_s3_disk_xml_inlines_credentials_and_embeds_prefix():
    cfg = _cfg(DestinationCfg(
        type="s3", endpoint="https://s3.example.com", bucket="bkt",
        prefix="prod", region="eu-central-1",
    ))
    creds = ResolvedCreds(access_key_id="AKID", secret_access_key="SECRET")
    xml = render_clickhouse_disk_xml(cfg, creds)
    assert "<type>s3</type>" in xml
    assert "<endpoint>https://s3.example.com/bkt/prod/ch/</endpoint>" in xml
    assert "<access_key_id>AKID</access_key_id>" in xml
    assert "<secret_access_key>SECRET</secret_access_key>" in xml
    assert "<region>eu-central-1</region>" in xml
    assert "<allowed_disk>precis_backups</allowed_disk>" in xml


def test_s3_endpoint_derived_from_region_when_unset():
    cfg = _cfg(DestinationCfg(type="s3", bucket="bkt", region="us-east-1"))
    assert s3_disk_endpoint(cfg) == "https://s3.us-east-1.amazonaws.com/bkt/ch/"


def test_s3_render_requires_resolved_credentials():
    cfg = _cfg(DestinationCfg(type="s3", bucket="bkt", region="us-east-1"))
    with pytest.raises(BackupConfigError, match="credentials"):
        render_clickhouse_disk_xml(cfg, None)


def test_xml_escapes_special_characters():
    creds = ResolvedCreds(access_key_id="A&B", secret_access_key="S<E>C")
    cfg = _cfg(DestinationCfg(type="s3", bucket="bkt", region="us-east-1"))
    xml = render_clickhouse_disk_xml(cfg, creds)
    assert "A&amp;B" in xml
    assert "S&lt;E&gt;C" in xml
