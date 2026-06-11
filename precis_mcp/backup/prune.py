# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Retention pruning — manifest-driven, per-store keep-N and age bounds.

A manifest is deleted only when every artifact it references is gone, so a
partially pruned bundle still lists correctly. On a WORM-locked destination
deletes are denied by design: pruning degrades to advisory (one warning) and
the bucket lifecycle policy owns retention.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from precis_mcp.backup.config import BackupConfig
from precis_mcp.backup.destination import BackupDestination, WormDeleteDenied
from precis_mcp.backup.manifest import BackupManifest

logger = logging.getLogger(__name__)


@dataclass
class PruneReport:
    deleted: list[str] = field(default_factory=list)
    worm_suppressed: int = 0


def _exists(dest: BackupDestination, key: str) -> bool:
    try:
        dest.size_of(key)
        return True
    except Exception:
        return False


def _run_started(manifest: BackupManifest) -> datetime:
    try:
        ts = datetime.fromisoformat(manifest.created_at)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def prune_bundles(
    cfg: BackupConfig,
    dest: BackupDestination,
    *,
    protect_run_id: str | None = None,
    now: datetime | None = None,
) -> PruneReport:
    now = now or datetime.now(timezone.utc)
    report = PruneReport()

    manifests: dict[str, BackupManifest] = {}
    for key in dest.list_keys("manifest/"):
        try:
            manifests[key] = BackupManifest.from_json(dest.get_bytes(key))
        except Exception:
            logger.warning("prune: unreadable manifest %s — left in place", key)

    # Per store: which runs survive keep-N ∩ age bound. Newest-first by run id
    # (the timestamp format sorts lexicographically).
    doomed_artifacts: dict[str, list[str]] = {}  # manifest key -> artifact keys to delete
    for store, retention in cfg.retention.items():
        with_artifact = [
            (mkey, m, m.artifact(store))
            for mkey, m in sorted(manifests.items(), key=lambda kv: kv[1].run_id, reverse=True)
            if m.artifact(store) is not None and m.artifact(store).key  # type: ignore[union-attr]
        ]
        cutoff = now - timedelta(days=retention.days) if retention.days else None
        for rank, (mkey, m, artifact) in enumerate(with_artifact):
            if m.run_id == protect_run_id:
                continue
            too_many = retention.keep is not None and rank >= retention.keep
            too_old = cutoff is not None and _run_started(m) < cutoff
            if too_many or too_old:
                doomed_artifacts.setdefault(mkey, []).append(artifact.key)  # type: ignore[union-attr]

    for mkey, artifact_keys in doomed_artifacts.items():
        manifest = manifests[mkey]
        deleted_here: set[str] = set()
        for akey in artifact_keys:
            try:
                dest.delete(akey)
                deleted_here.add(akey)
                report.deleted.append(akey)
            except WormDeleteDenied:
                report.worm_suppressed += 1

        remaining = [
            a for a in manifest.artifacts
            if a.key and a.key not in deleted_here and _exists(dest, a.key)
        ]
        # The manifest goes only when no artifact it references survives.
        if not remaining:
            try:
                dest.delete(mkey)
                report.deleted.append(mkey)
            except WormDeleteDenied:
                report.worm_suppressed += 1

    if report.worm_suppressed:
        level = logging.INFO if cfg.expect_worm else logging.WARNING
        logger.log(
            level,
            "prune: %d delete(s) denied — destination appears WORM-locked; "
            "pruning is advisory, configure a bucket lifecycle policy for retention",
            report.worm_suppressed,
        )
    if report.deleted:
        logger.info("prune: removed %d object(s)", len(report.deleted))
    return report
