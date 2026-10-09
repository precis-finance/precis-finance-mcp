# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared workbook-lifecycle helpers for the Précis Excel emitters.

``target='new'`` / ``target='append'`` semantics are uniform across all
Précis emitters (statement, metric, extract): the only difference
between emitters is *what they write into the sheet*, not how they
wrangle the workbook before and after.

This module encapsulates that wrangling so each emitter stays focused
on its own layout.

See docs/excel_agent_research_2026.md §6.5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from openpyxl import Workbook, load_workbook

from precis_mcp.excel.meta import (
    CURRENT_META_VERSION,
    META_SHEET_NAME,
    read_meta,
    write_meta,
)


class WorkbookLifecycleError(Exception):
    """Raised when target/overwrite constraints fail at open time."""


def prepare_workbook(
    path: Path,
    target: str,
    overwrite: bool,
    requested_sheet: str,
) -> tuple[Workbook, dict[str, Any] | None, list[str]]:
    """Open or create a workbook ready for a new sheet to be added.

    Args:
        path: Destination file path.
        target: ``"new"`` creates a fresh workbook (raises if ``path``
            exists and ``overwrite`` is False). ``"append"`` loads the
            existing workbook (raises if ``path`` does not exist).
        overwrite: On ``target="new"`` permits clobbering an existing
            file. On ``target="append"`` permits replacing a colliding
            sheet named ``requested_sheet``.
        requested_sheet: Final sheet name the caller intends to create.
            Used to detect collisions on append.

    Returns:
        ``(wb, existing_meta, pre_existing_sheet_names)`` where
        ``pre_existing_sheet_names`` excludes the meta sheet and the
        requested sheet (the latter because we may have just dropped
        it under overwrite). Both are empty on ``target="new"``.
    """
    if target == "new":
        if path.exists() and not overwrite:
            raise WorkbookLifecycleError(
                f"File {path} already exists — pass overwrite=True to replace"
            )
        wb = Workbook()
        default = wb.active
        if default is not None:
            wb.remove(default)
        return wb, None, []

    if target == "append":
        if not path.exists():
            raise WorkbookLifecycleError(
                f"File {path} does not exist — can't append. "
                f"Use target='new' to create a new workbook."
            )
        wb = load_workbook(str(path))
        if requested_sheet in wb.sheetnames:
            if not overwrite:
                raise WorkbookLifecycleError(
                    f"Sheet {requested_sheet!r} already exists in "
                    f"{path.name} — pass overwrite=True to replace it"
                )
            del wb[requested_sheet]
        try:
            existing_meta = read_meta(wb)
        except Exception as e:
            raise WorkbookLifecycleError(
                f"existing {path.name}/_precis_meta is malformed: {e}"
            ) from e
        pre_existing = [
            s for s in wb.sheetnames
            if s != META_SHEET_NAME and s != requested_sheet
        ]
        return wb, existing_meta, pre_existing

    raise WorkbookLifecycleError(
        f"target={target!r} not in ('new', 'append')"
    )


def merge_meta(
    *,
    existing_meta: dict[str, Any] | None,
    pre_existing_sheet_names: list[str],
    new_sheet_name: str,
    new_sheet_entry: Mapping[str, Any],
    workbook_kind_default: str = "report",
    catalogue_version: str | None = None,
    user_id: str | None = None,
    agent_model: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Merge a new sheet entry into fresh-or-existing meta.

    Three cases:

    1. ``existing_meta is None`` and no pre-existing sheets (fresh
       ``target=new``): build a clean meta with just this sheet.
    2. ``existing_meta is None`` and pre-existing sheets present
       (``target=append`` onto a non-Précis workbook): build new meta,
       tag pre-existing sheets as ``kind: user`` so refresh leaves them
       alone, then add the new sheet.
    3. ``existing_meta is not None`` (``target=append`` onto an already-
       Précis workbook): keep existing sheet entries verbatim, bump
       ``generated_at``, refresh ``generated_by`` / ``catalogue_version``
       if provided, then add the new sheet.

    ``workbook_kind_default`` is used only in cases 1 and 2 — case 3
    preserves the existing ``workbook_kind``.
    """
    if generated_at is None:
        generated_at = datetime.now(tz=timezone.utc)
    elif generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    else:
        generated_at = generated_at.astimezone(timezone.utc)
    ts = generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    if existing_meta is None:
        meta: dict[str, Any] = {
            "precis_meta_version": CURRENT_META_VERSION,
            "generated_at": ts,
            "generated_by": {
                "user_id": user_id,
                "agent_model": agent_model,
            },
            "catalogue_version": catalogue_version,
            "workbook_kind": workbook_kind_default,
            "sheets": {},
        }
        for name in pre_existing_sheet_names:
            meta["sheets"][name] = {"kind": "user"}
    else:
        meta = dict(existing_meta)
        meta["generated_at"] = ts
        if user_id or agent_model:
            gb = dict(meta.get("generated_by") or {})
            if user_id:
                gb["user_id"] = user_id
            if agent_model:
                gb["agent_model"] = agent_model
            meta["generated_by"] = gb
        if catalogue_version:
            meta["catalogue_version"] = catalogue_version
        sheets = dict(meta.get("sheets") or {})
        meta["sheets"] = sheets

    meta["sheets"][new_sheet_name] = dict(new_sheet_entry)
    return meta


def finalize_workbook(
    wb: Workbook,
    path: Path,
    meta: Mapping[str, Any],
) -> None:
    """Write meta into the workbook and save it to *path*."""
    write_meta(wb, meta)
    wb.save(str(path))
