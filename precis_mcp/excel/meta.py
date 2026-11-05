# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""``_precis_meta`` read/write for Précis-generated xlsx workbooks (slice 3).

Every xlsx Précis produces carries a hidden sheet named ``_precis_meta``
with a single cell A1 holding a YAML document (schema §6.3 of
docs/excel_agent_research_2026.md). The YAML captures:

* Who/what/when generated it (``generated_at``, ``generated_by``,
  ``catalogue_version``).
* Which tool produced each financial region (``sheets.<name>.regions[].source``).
  Non-financial exports may carry sheet-level provenance.
* Layout-specific shape hints: generated report regions record header/data
  offsets; extract regions also carry row identities and refresh policy.

The hidden-sheet form is deliberate: it round-trips through openpyxl,
LibreOffice, Excel desktop, and Excel Online with no special handling.
If it ever gets corrupted in the wild the migration target is a custom
XML part via the F4 OOXML pack/unpack path — documented in §6.2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import yaml
from openpyxl.workbook import Workbook

META_SHEET_NAME = "_precis_meta"
CURRENT_META_VERSION = 1
SUPPORTED_META_VERSIONS: frozenset[int] = frozenset({1})

VALID_SHEET_KINDS: frozenset[str] = frozenset(
    {"extract", "report", "roundtrip", "user"}
)
VALID_WORKBOOK_KINDS: frozenset[str] = frozenset(
    {"extract", "report", "roundtrip"}
)


class MetaError(Exception):
    """Raised when ``_precis_meta`` is present but malformed or unsupported."""


def build_minimal_meta(
    *,
    workbook_kind: str = "report",
    catalogue_version: str | None = None,
    user_id: str | None = None,
    agent_model: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a meta dict without a ``source`` — when the caller doesn't
    know the tool/params (e.g. hierarchy_to_excel, ad-hoc exports).

    A minimal meta still identifies the file as Précis-generated and
    pins the schema version, but ``refresh_workbook`` will refuse it
    with a "no source" error. That's the correct failure mode.
    """
    if workbook_kind not in VALID_WORKBOOK_KINDS:
        raise MetaError(
            f"workbook_kind={workbook_kind!r} not in "
            f"{sorted(VALID_WORKBOOK_KINDS)}"
        )
    return {
        "precis_meta_version": CURRENT_META_VERSION,
        "generated_at": _iso_utc(generated_at),
        "generated_by": {
            "user_id": user_id,
            "agent_model": agent_model,
        },
        "catalogue_version": catalogue_version,
        "workbook_kind": workbook_kind,
    }


def build_report_meta(
    *,
    tool: str,
    params: Mapping[str, Any],
    sheet_name: str,
    catalogue_version: str | None = None,
    user_id: str | None = None,
    agent_model: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a meta dict for a single-sheet styled report.

    Shape metadata is deliberately omitted — reports are refreshed by
    re-running ``tool`` with ``params`` and rewriting the sheet, not by
    identity-matched row updates (spec §6.7). Extracts (slice 4) carry
    the full shape + row_identities + refresh_policy block.
    """
    meta = build_minimal_meta(
        workbook_kind="report",
        catalogue_version=catalogue_version,
        user_id=user_id,
        agent_model=agent_model,
        generated_at=generated_at,
    )
    meta["sheets"] = {
        sheet_name: {
            "kind": "report",
            "source": {
                "tool": tool,
                "params": dict(params),
            },
        },
    }
    return meta


def _normalise_region(region: Mapping[str, Any]) -> dict[str, Any]:
    """Validate + copy one region entry: ``{range_name, source:{tool, params}}``."""
    range_name = region.get("range_name")
    if not range_name or not isinstance(range_name, str):
        raise MetaError("region requires a non-empty string range_name")
    source = region.get("source")
    if not isinstance(source, Mapping) or not source.get("tool"):
        raise MetaError(f"region {range_name!r}.source requires at least a tool")
    normalised = {
        "range_name": range_name,
        "source": {
            "tool": source["tool"],
            "params": dict(source.get("params", {})),
        },
    }
    for key in ("state", "layout", "origin", "table_name", "named_ranges", "shape", "presentation", "placement", "refresh"):
        if key in region:
            normalised[key] = region[key]
    return normalised


def build_regions_meta(
    *,
    sheet_name: str,
    regions: list[Mapping[str, Any]],
    workbook_kind: str = "report",
    catalogue_version: str | None = None,
    user_id: str | None = None,
    agent_model: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a meta dict for a sheet holding one or more refreshable **regions**.

    The per-region successor to :func:`build_report_meta`. Instead of a single
    per-sheet ``source``, the sheet entry carries a ``regions`` list — each
    anchored to a **named range** (a defined name / Excel Table, *not* a literal
    address, so the link survives row / column inserts and moves) and carrying
    its own ``{tool, params}`` source. This is the STATIC-state encoding of the
    add-in's region state machine: one entry per ``=PRECIS.STATEMENT`` /
    ``=PRECIS.METRIC`` region that has been resolved to values. See
    docs/excel_region_transitions_spec.md.

    Financial refresh and activation require the region source; no sheet-source
    fallback or migration of pre-region workbooks is supported.
    """
    meta = build_minimal_meta(
        workbook_kind=workbook_kind,
        catalogue_version=catalogue_version,
        user_id=user_id,
        agent_model=agent_model,
        generated_at=generated_at,
    )
    sheet_kind = workbook_kind if workbook_kind in VALID_SHEET_KINDS else "report"
    meta["sheets"] = {
        sheet_name: {
            "kind": sheet_kind,
            "regions": [_normalise_region(r) for r in regions],
        },
    }
    return meta


def stamp_report_meta(
    wb: Workbook,
    sheet_name: str,
    meta_source: Mapping[str, Any] | None,
) -> None:
    """Convenience wrapper for emitters: build + write meta in one call.

    When ``meta_source`` is ``None`` a minimal meta (no source, no sheets
    entry) is stamped so the file is still tagged as Précis-generated. A
    populated ``meta_source`` must carry at least ``tool``; other keys
    (``params``, ``catalogue_version``, ``user_id``, ``agent_model``) are
    optional.
    """
    if meta_source is None:
        meta = build_minimal_meta()
    else:
        meta = build_report_meta(
            tool=meta_source["tool"],
            params=meta_source.get("params", {}),
            sheet_name=sheet_name,
            catalogue_version=meta_source.get("catalogue_version"),
            user_id=meta_source.get("user_id"),
            agent_model=meta_source.get("agent_model"),
        )
    write_meta(wb, meta)


def write_meta(wb: Workbook, meta: Mapping[str, Any]) -> None:
    """Stamp *meta* into the workbook's hidden ``_precis_meta`` sheet.

    Replaces any existing meta sheet so refresh callers can rewrite it
    in-place. The sheet is hidden and positioned at the end of the
    sheet order (``create_sheet`` appends by default).
    """
    _validate_meta(meta)
    yaml_text = yaml.safe_dump(
        dict(meta),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if len(yaml_text.encode("utf-16-le")) // 2 > 32767:
        raise MetaError("Workbook provenance exceeds Excel's cell limit; reduce the extract size")
    if META_SHEET_NAME in wb.sheetnames:
        del wb[META_SHEET_NAME]
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = yaml_text
    ws.sheet_state = "hidden"


def read_meta(wb: Workbook) -> dict[str, Any] | None:
    """Return the parsed meta dict, or ``None`` if the sheet is absent.

    Raises:
        MetaError: sheet present but YAML is malformed, A1 is empty, or
            the schema version is not in ``SUPPORTED_META_VERSIONS``.
    """
    if META_SHEET_NAME not in wb.sheetnames:
        return None
    ws = wb[META_SHEET_NAME]
    raw = ws["A1"].value
    if not isinstance(raw, str) or not raw.strip():
        raise MetaError(f"{META_SHEET_NAME}!A1 is empty or non-string")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise MetaError(f"{META_SHEET_NAME}!A1 has malformed YAML: {e}") from e
    if not isinstance(parsed, dict):
        raise MetaError(f"{META_SHEET_NAME}!A1 YAML must be a mapping")
    _validate_meta(parsed)
    return parsed


def _validate_meta(meta: Mapping[str, Any]) -> None:
    """Enforce the minimum schema shape. Intentionally lenient — we
    only check fields whose absence would make refresh unsafe."""
    version = meta.get("precis_meta_version")
    if version is None:
        raise MetaError("_precis_meta missing precis_meta_version")
    if version not in SUPPORTED_META_VERSIONS:
        raise MetaError(
            f"_precis_meta version {version!r} not in "
            f"SUPPORTED_META_VERSIONS={sorted(SUPPORTED_META_VERSIONS)}; "
            f"regenerate the workbook"
        )
    wb_kind = meta.get("workbook_kind")
    if wb_kind is not None and wb_kind not in VALID_WORKBOOK_KINDS:
        raise MetaError(
            f"workbook_kind={wb_kind!r} not in {sorted(VALID_WORKBOOK_KINDS)}"
        )
    sheets = meta.get("sheets")
    if sheets is None:
        return
    if not isinstance(sheets, Mapping):
        raise MetaError("sheets must be a mapping")
    for name, entry in sheets.items():
        if not isinstance(entry, Mapping):
            raise MetaError(f"sheets[{name!r}] must be a mapping")
        kind = entry.get("kind")
        if kind is not None and kind not in VALID_SHEET_KINDS:
            raise MetaError(
                f"sheets[{name!r}].kind={kind!r} not in "
                f"{sorted(VALID_SHEET_KINDS)}"
            )
        regions = entry.get("regions")
        if regions is not None:
            if not isinstance(regions, list):
                raise MetaError(f"sheets[{name!r}].regions must be a list")
            for i, region in enumerate(regions):
                if not isinstance(region, Mapping):
                    raise MetaError(f"sheets[{name!r}].regions[{i}] must be a mapping")
                rn = region.get("range_name")
                if not rn or not isinstance(rn, str):
                    raise MetaError(
                        f"sheets[{name!r}].regions[{i}] requires a string range_name"
                    )
                src = region.get("source")
                if src is not None and (
                    not isinstance(src, Mapping) or not src.get("tool")
                ):
                    raise MetaError(
                        f"sheets[{name!r}].regions[{i}].source requires a tool"
                    )


def _iso_utc(dt: datetime | None) -> str:
    """Format *dt* (or now) as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC."""
    if dt is None:
        dt = datetime.now(tz=timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
