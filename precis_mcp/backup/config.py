# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""backup.yml — load and validate the declarative backup config.

The file holds credential *references* (env-var prefixes resolved through the
standard ``*_FILE`` secret convention), never credential values — it lives in
the git-tracked instance repo. The validator rejects anything that looks like
an inline secret.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from precis_mcp.backup import BackupConfigError

STORES = ("postgres", "clickhouse", "instance", "files")
SCOPED_STORES = ("postgres", "clickhouse", "files")

_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Keys that would mean a credential value was written into the config file.
_INLINE_SECRET_KEYS = {
    "access_key_id",
    "secret_access_key",
    "password",
    "token",
    "secret",
}


@dataclass(frozen=True)
class DestinationCfg:
    type: str  # "local" | "s3"
    path: str | None = None
    endpoint: str | None = None
    bucket: str | None = None
    prefix: str = ""
    region: str | None = None


@dataclass(frozen=True)
class RetentionCfg:
    keep: int | None = None
    days: int | None = None


@dataclass(frozen=True)
class CredentialRefs:
    writer: str | None = None
    reader: str | None = None


@dataclass(frozen=True)
class ResolvedCreds:
    access_key_id: str
    secret_access_key: str


@dataclass(frozen=True)
class BackupConfig:
    mode: str
    destination: DestinationCfg
    credentials: CredentialRefs
    schedule_cron: str
    retention: dict[str, RetentionCfg]
    scope: dict[str, str]
    kms_key_id: str | None = None
    expect_worm: bool = False
    alert_webhook_url: str | None = None
    notices: tuple[str, ...] = field(default=())


def default_backup_config_path() -> Path:
    from precis_mcp.clickhouse_init import default_instance_dir

    return default_instance_dir() / "backup.yml"


def resolve_credentials(ref: str) -> ResolvedCreds:
    """Resolve an env-var-prefix credential ref, e.g. ``BACKUP_WRITER`` →
    ``BACKUP_WRITER_ACCESS_KEY_ID`` + ``BACKUP_WRITER_SECRET_ACCESS_KEY``.
    The ``*_FILE`` variants are already resolved by `precis_mcp.secrets`."""
    key_id = os.getenv(f"{ref}_ACCESS_KEY_ID", "")
    secret = os.getenv(f"{ref}_SECRET_ACCESS_KEY", "")
    if not key_id or not secret:
        raise BackupConfigError(
            f"credential ref {ref!r}: set {ref}_ACCESS_KEY_ID and "
            f"{ref}_SECRET_ACCESS_KEY (or their _FILE variants)"
        )
    return ResolvedCreds(access_key_id=key_id, secret_access_key=secret)


def _reject_inline_secrets(node: object, path: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            where = f"{path}.{k}" if path else str(k)
            if str(k).lower() in _INLINE_SECRET_KEYS:
                raise BackupConfigError(
                    f"{where}: backup.yml must hold credential references, "
                    "never credential values — use credentials.writer/reader "
                    "env-var prefixes"
                )
            _reject_inline_secrets(v, where)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_inline_secrets(v, f"{path}[{i}]")


def _require_mapping(node: object, where: str) -> dict:
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise BackupConfigError(f"{where}: expected a mapping")
    return node


def _check_keys(node: dict, allowed: set[str], where: str) -> None:
    unknown = set(node) - allowed
    if unknown:
        raise BackupConfigError(
            f"{where}: unknown key(s) {sorted(unknown)!r} — allowed: {sorted(allowed)}"
        )


def _parse_destination(raw: dict) -> DestinationCfg:
    _check_keys(raw, {"type", "path", "endpoint", "bucket", "prefix", "region"}, "destination")
    dtype = raw.get("type")
    if dtype not in ("local", "s3"):
        raise BackupConfigError("destination.type: must be 'local' or 's3'")
    if dtype == "local":
        if not raw.get("path"):
            raise BackupConfigError("destination.path: required for type 'local'")
    else:
        if not raw.get("bucket"):
            raise BackupConfigError("destination.bucket: required for type 's3'")
        if not raw.get("endpoint") and not raw.get("region"):
            raise BackupConfigError(
                "destination: type 's3' needs an endpoint (MinIO/R2) or a region (AWS)"
            )
    return DestinationCfg(
        type=dtype,
        path=raw.get("path"),
        endpoint=raw.get("endpoint"),
        bucket=raw.get("bucket"),
        prefix=str(raw.get("prefix") or "").strip("/"),
        region=raw.get("region"),
    )


def _parse_credentials(raw: dict) -> CredentialRefs:
    _check_keys(raw, {"writer", "reader"}, "credentials")
    for role in ("writer", "reader"):
        ref = raw.get(role)
        if ref is not None and not _ENV_PREFIX_RE.match(str(ref)):
            raise BackupConfigError(
                f"credentials.{role}: {ref!r} is not an env-var prefix "
                "(UPPER_SNAKE_CASE, e.g. BACKUP_WRITER)"
            )
    return CredentialRefs(writer=raw.get("writer"), reader=raw.get("reader"))


def _parse_schedule(raw: dict, mode: str) -> str:
    _check_keys(raw, {"cron", "incremental"}, "schedule")
    if "incremental" in raw and mode == "dump":
        raise BackupConfigError(
            "schedule.incremental: incremental backups are a pitr-tier feature; "
            "remove the key in dump mode"
        )
    cron = raw.get("cron")
    if not cron or not isinstance(cron, str):
        raise BackupConfigError("schedule.cron: required (5-field cron expression)")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        raise BackupConfigError(f"schedule.cron: invalid expression {cron!r}: {exc}") from exc
    return cron


def _parse_retention(raw: dict, mode: str) -> dict[str, RetentionCfg]:
    if "wal_window_days" in raw and mode == "dump":
        raise BackupConfigError(
            "retention.wal_window_days: WAL retention is a pitr-tier setting; "
            "remove the key in dump mode"
        )
    _check_keys(raw, set(STORES), "retention")
    out: dict[str, RetentionCfg] = {}
    for store, node in raw.items():
        node = _require_mapping(node, f"retention.{store}")
        _check_keys(node, {"keep", "days"}, f"retention.{store}")
        keep, days = node.get("keep"), node.get("days")
        for name, val in (("keep", keep), ("days", days)):
            if val is not None and (not isinstance(val, int) or val < 1):
                raise BackupConfigError(f"retention.{store}.{name}: must be a positive integer")
        if keep is None and days is None:
            raise BackupConfigError(f"retention.{store}: set keep, days, or both")
        out[store] = RetentionCfg(keep=keep, days=days)
    return out


def _parse_scope(raw: dict) -> dict[str, str]:
    _check_keys(raw, set(SCOPED_STORES), "scope")
    out = {"postgres": "managed", "clickhouse": "managed", "files": "external"}
    for store, value in raw.items():
        if value not in ("managed", "external"):
            raise BackupConfigError(f"scope.{store}: must be 'managed' or 'external'")
        out[store] = value
    return out


def load_backup_config(path: Path | None = None) -> BackupConfig:
    path = path or default_backup_config_path()
    if not Path(path).is_file():
        raise BackupConfigError(f"backup config not found: {path}")
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BackupConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BackupConfigError(f"{path}: expected a top-level mapping")

    _check_keys(
        raw,
        {"mode", "destination", "credentials", "schedule", "retention",
         "scope", "encryption", "expect_worm", "alert"},
        "backup.yml",
    )
    _reject_inline_secrets(raw)

    mode = raw.get("mode")
    if mode == "pitr":
        raise BackupConfigError(
            "mode: pitr is parsed but not yet supported in this release; use mode: dump"
        )
    if mode != "dump":
        raise BackupConfigError("mode: must be 'dump' (pitr not yet supported)")

    destination = _parse_destination(_require_mapping(raw.get("destination"), "destination"))
    credentials = _parse_credentials(_require_mapping(raw.get("credentials"), "credentials"))
    notices: list[str] = []
    if destination.type == "s3" and not credentials.writer:
        raise BackupConfigError(
            "credentials.writer: required for an s3 destination "
            "(env-var prefix, e.g. BACKUP_WRITER)"
        )
    if destination.type == "local" and (credentials.writer or credentials.reader):
        notices.append("credentials are ignored for a local destination")

    schedule_cron = _parse_schedule(_require_mapping(raw.get("schedule"), "schedule"), mode)
    retention = _parse_retention(_require_mapping(raw.get("retention"), "retention"), mode)
    scope = _parse_scope(_require_mapping(raw.get("scope"), "scope"))

    encryption = _require_mapping(raw.get("encryption"), "encryption")
    _check_keys(encryption, {"kms_key_id"}, "encryption")
    kms_key_id = encryption.get("kms_key_id")
    if kms_key_id is not None and destination.type == "local":
        notices.append("encryption.kms_key_id is ignored for a local destination")

    alert = _require_mapping(raw.get("alert"), "alert")
    _check_keys(alert, {"webhook_url"}, "alert")

    expect_worm = bool(raw.get("expect_worm", False))

    return BackupConfig(
        mode=mode,
        destination=destination,
        credentials=credentials,
        schedule_cron=schedule_cron,
        retention=retention,
        scope=scope,
        kms_key_id=kms_key_id,
        expect_worm=expect_worm,
        alert_webhook_url=alert.get("webhook_url"),
        notices=tuple(notices),
    )
