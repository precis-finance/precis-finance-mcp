# SPDX-License-Identifier: Elastic-2.0
"""Discovery and bounded formula → static conversion, independent of file/auth APIs.

The caller supplies an authorized full-precision dispatch, instance origin and
immutable input bytes. This module never trusts workbook identity as authority.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import tempfile

from precis_mcp.excel.formulas import ConversionError, contains_precis_function, parse_financial_formula, validate_source
from precis_mcp.excel.meta import build_minimal_meta
from precis_mcp.excel.ooxml import XlsxPackage, MAX_CELLS, MAX_REGIONS, address, overlaps, rectangle, sheet_ref, tag


def select_regions(regions, scope=None, *, explicit=False):
    if scope is None:
        if explicit:
            raise ConversionError("scope_required", "Conversion requires region_ids, sheets, or workbook=true scope.")
        return regions
    if not isinstance(scope, dict) or len(scope) != 1:
        raise ConversionError("invalid_scope", "Scope must contain exactly one of region_ids, sheets, workbook.")
    if scope == {"workbook": True}:
        return regions
    key = next(iter(scope))
    values = scope[key]
    if key not in {"region_ids", "sheets"} or not isinstance(values, list) or not values or len(values) > MAX_REGIONS or any(not isinstance(v, str) for v in values):
        raise ConversionError("invalid_scope", "Select a non-empty list of region_ids or sheets, or workbook=true.")
    column = "region_id" if key == "region_ids" else "sheet"
    unknown = set(values)-{r[column] for r in regions}
    if unknown:
        raise ConversionError("unknown_scope", "Scope contains unknown or stale regions/sheets; run discovery again.")
    return [r for r in regions if r[column] in values]


def _check_origin(record, origin, *, required=False):
    recorded = record.get("origin")
    if required and not recorded:
        raise ConversionError("missing_origin", "The saved region has no originating Précis instance.", "Regenerate the export and convert it using the current add-in, then save and upload again.")
    if recorded and recorded.rstrip("/") != origin.rstrip("/"):
        raise ConversionError("origin_mismatch", "This region came from a different Précis instance.", "Use the instance that generated the workbook.")


def _check_footer(marker, *, live=False):
    shape = marker if live else marker.get("shape", {})
    if shape.get("footer_rows") != 1 or not live and shape.get("header_row") != 1:
        raise ConversionError("unsupported_layout", "This region does not use the current headers, data and footer layout.", "Regenerate the export with the current Précis version.")


def _spill_reference(text, owner_sheet, sheet, anchor, package=None, seen=frozenset()):
    """Recognize OOXML spellings of direct # and ANCHORARRAY references."""
    text = re.sub(r'"(?:[^"]|"")*"', '""', text or "")
    text = re.sub(r"(?:_xlfn\.)?ANCHORARRAY\(([^()]+)\)", r"\1#", text, flags=re.I)
    pattern = r"(?<![\w.])(?:(?:'((?:[^']|'')+)'|([A-Za-z_][\w .]*))!)?(\$?[A-Za-z]{1,3}\$?\d+)#"
    for match in re.finditer(pattern, text, re.I):
        target_sheet = match[1].replace("''", "'") if match[1] else match[2] or owner_sheet
        if target_sheet.casefold() == sheet.casefold() and match[3].replace("$", "").upper() == anchor.upper():
            return True
    # A name pointing at the anchor can also be used as Name#. Follow only
    # simple defined-name references; unresolved dynamic expressions are refused.
    if package is not None:
        for match in re.finditer(r"(?<![\w.$])(?:(?:'((?:[^']|'')+)'|([A-Za-z_][\w .]*))!)?([A-Za-z_\\][\w.\\]*)#", text):
            name = match[3]
            if re.fullmatch(r"[A-Za-z]{1,3}\d+",name):
                continue
            named_sheet = (match[1].replace("''", "'") if match[1] else match[2]) or owner_sheet
            named_sheet = next((s for s in package.sheets if s.casefold() == named_sheet.casefold()), None)
            if named_sheet is None:
                return True
            definitions = [node for node in package.names() if node.get("name", "").casefold() == name.casefold()]
            local = [node for node in definitions if node.get("localSheetId") == str(list(package.sheets).index(named_sheet))]
            definitions = local or [node for node in definitions if node.get("localSheetId") is None]
            if not definitions or name.casefold() in seen:
                return True
            for definition in definitions:
                value = (definition.text or "").lstrip("=")
                if not re.fullmatch(r"(?:(?:'(?:[^']|'')+'|[\w .]+)!)?\$?[A-Za-z]{1,3}\$?\d+",value):
                    return True
                if _spill_reference(value+"#",named_sheet,sheet,anchor,package,seen | {name.casefold()}):
                    return True
    return False


def _dependent_names(package, sheet, anchor, marker):
    """Recognize generated boundary/value names; refuse other spill consumers."""
    names = []
    for node in package.names():
        text = node.text or ""
        local = node.get("localSheetId")
        owner = list(package.sheets)[int(local)] if local is not None else sheet
        if not _spill_reference(text, owner, sheet, anchor, package):
            continue
        name = node.get("name", "")
        if marker and name == marker.get("range_name"):
            # Boundary must name this spill alone, not an expression using it.
            normalized = text.lstrip("=")
            normalized = re.sub(r"(?:_xlfn\.)?ANCHORARRAY\(([^()]+)\)", r"\1#", normalized, flags=re.I)
            if normalized.replace("$", "") not in {sheet_ref(sheet, anchor+"#"), sheet+"!"+anchor+"#"}:
                raise ConversionError("spill_dependency", f"Boundary name {name!r} is not a direct spill reference.")
            names.append((name, None))
            continue
        # Generated value names exclude the context footer from the live spill.
        normalized = re.sub(r"(?:_xlfn\.)?ANCHORARRAY\(([^()]+)\)", r"\1#", text.lstrip("="), flags=re.I)
        match = re.fullmatch(r"INDEX\((.+),2,(\d+)\):INDEX\(\1,ROWS\(\1\)-1,(\d+)\)", normalized, re.I)
        if name.startswith("ext_") and match and match[2] == match[3] and marker:
            names.append((name, int(match[2])))
        else:
            raise ConversionError("spill_dependency", f"Defined name {name!r} depends on the live spill.", "Replace the spill-dependent name with ordinary ranges in Excel before converting.")
    for owner_sheet in package.sheets:
        for coord, cell in package.cells(owner_sheet).items():
            formula = cell.find(tag("f"))
            if formula is not None and not (owner_sheet == sheet and coord == anchor) and _spill_reference(formula.text, owner_sheet, sheet, anchor, package):
                raise ConversionError("spill_dependency", f"Formula {owner_sheet}!{coord} depends on {sheet}!{anchor}#.", "Replace the spill reference with an ordinary range or generated value name in Excel before converting.")
        for node in package.sheet(owner_sheet).iter():
            if isinstance(node.tag, str) and node.tag.rsplit("}",1)[-1] in {"formula", "formula1", "formula2"} and _spill_reference(node.text, owner_sheet, sheet, anchor, package):
                raise ConversionError("spill_dependency", f"A validation or formatting rule on {owner_sheet!r} depends on this live spill.", "Replace the rule's spill reference with an ordinary range in Excel before converting.")
    # Other supported package consumers (e.g. Table calculated columns and
    # chart series) use formula elements outside worksheet cell data.
    worksheet_parts = set(package.sheets.values())
    for part in package.parts:
        if part in worksheet_parts or not part.endswith(".xml") or not part.startswith(("xl/tables/", "xl/charts/")):
            continue
        for node in package.tree(part).iter():
            if isinstance(node.tag,str) and node.tag.rsplit("}",1)[-1] in {"f","calculatedColumnFormula","totalsRowFormula"} and _spill_reference(node.text, sheet, sheet, anchor, package):
                raise ConversionError("spill_dependency", "A chart or Table formula depends on this live spill.", "Replace that object's spill reference in Excel before converting.")
    return names


def discover_regions(blob: bytes, *, origin: str, scope=None):
    package = XlsxPackage(blob)
    fingerprint = sha256(blob).hexdigest()
    records = []
    metadata = (package.meta or {}).get("sheets", {})
    for sheet in package.sheets:
        if sheet == "_precis_meta":
            continue
        markers = metadata.get(sheet, {}).get("regions", [])
        seen = set()
        for coord, cell in package.cells(sheet).items():
            formula = cell.find(tag("f"))
            if formula is None or not contains_precis_function(formula.text or ""):
                continue
            key = "live:"+sheet+"!"+coord
            record = {"region_id":sha256((fingerprint+key).encode()).hexdigest()[:24], "sheet":sheet, "anchor":coord, "state":"live", "layout":None, "eligible_directions":[], "diagnostics":[]}
            try:
                definition = parse_financial_formula(formula.text, lambda ref: package.resolve_scalar(sheet, ref))
                record["layout"] = definition.layout
                record["_definition"] = definition
                if formula.get("t") != "array" or not formula.get("ref"):
                    raise ConversionError("missing_spill_bounds", f"No reliable saved array/spill extent exists for {sheet}!{coord}.", "Open the workbook in Excel, let the formula spill successfully, save it and upload again.")
                bounds = rectangle(formula.get("ref"))
                if bounds[:2] != rectangle(coord)[:2]:
                    raise ConversionError("invalid_spill_bounds", "Saved array bounds do not start at the formula anchor.")
                record["bounds"] = address(bounds)
                matching = [m for m in markers if m.get("state") == "live" and _marker_anchor(package, m, sheet) == coord]
                if len(matching) > 1:
                    raise ConversionError("ambiguous_region", "Multiple saved live markers identify the same formula.")
                marker = matching[0] if matching else None
                if marker:
                    _check_origin(marker, origin, required=True)
                    _check_footer(marker, live=True)
                    seen.add(marker["range_name"])
                record["_marker"] = marker
                record["_names"] = _dependent_names(package, sheet, coord, marker)
                package.check_placement(sheet, bounds, bounds, anchor=coord)
                record["eligible_directions"] = ["formula_to_static"]
            except ConversionError as exc:
                record["diagnostics"].append(exc.diagnostic())
            records.append(record)
        for marker in markers:
            if marker.get("range_name") in seen:
                continue
            state = marker.get("state", "static")
            if state == "live":
                # It was already represented as a formula candidate, including
                # a blocked candidate. Or it is an orphaned/stale marker.
                anchor = _marker_anchor(package, marker, sheet)
                if any(r["sheet"] == sheet and r["anchor"] == anchor for r in records):
                    continue
            key = "static:"+sheet+":"+marker.get("range_name", "")
            record = {"region_id":sha256((fingerprint+key).encode()).hexdigest()[:24], "sheet":sheet, "anchor":None, "state":state, "layout":marker.get("layout"), "eligible_directions":[], "diagnostics":[], "_marker":marker}
            try:
                bounds = package.name_bounds(marker.get("range_name"), sheet)
                record["bounds"], record["anchor"] = address(bounds), address(bounds).split(":")[0]
                _check_origin(marker, origin, required=True)
                _check_footer(marker)
                validate_source(marker.get("source"))
                if state != "static":
                    raise ConversionError("orphaned_marker", "Saved live/recovery metadata has no supported financial formula anchor.")
                record["diagnostics"] = [{"code":"direction_not_implemented", "reason":"Agent-side static → formula is not implemented.", "remedy":"Use Convert to live formula in the Excel add-in."}]
            except ConversionError as exc:
                record["diagnostics"].append(exc.diagnostic())
            records.append(record)
    if len(records) > MAX_REGIONS:
        raise ConversionError("region_limit", "Workbook exceeds the 64-region limit.")
    return package, select_regions(records, scope)


def _marker_anchor(package, marker, sheet):
    for name in package.names():
        if name.get("name") != marker.get("range_name"):
            continue
        text = re.sub(r"(?:_xlfn\.)?ANCHORARRAY\(([^()]+)\)", r"\1#", name.text or "", flags=re.I)
        # Excel omits quotes for simple sheet names when saving defined names.
        prefix = r"(?:"+re.escape(sheet_ref(sheet, ""))+"|"+re.escape(sheet+"!")+")"
        match = re.fullmatch(r"=?"+prefix+r"(\$?[A-Z]+\$?\d+)#", text, re.I)
        if match:
            return match[1].replace("$", "").upper()
    return None


def public_regions(records):
    return [{k:v for k,v in record.items() if not k.startswith("_")} for record in records]


def render_region(result, source, layout):
    """The actual shared exporters are the sole source of formatting rules."""
    if not result.get("rows") or not result.get("scenarios"):
        raise ConversionError("empty_result", "The fresh query has no renderable financial rows; no cells were replaced.")
    if len(result["rows"])*(len(result["scenarios"])+len(result.get("dimensions",[]))+2) > MAX_CELLS:
        raise ConversionError("region_limit", "The fresh query exceeds the supported rendering cell limit.")
    with tempfile.TemporaryDirectory(prefix="precis-render-") as directory:
        path = Path(directory)/"region.xlsx"
        if layout == "extract":
            from precis_mcp.excel.extract_emit import extract_to_excel
            extract_to_excel(result, path, source_tool=source["tool"], source_params=source["params"])
        else:
            from precis_mcp.excel.snapshot_export import write_snapshot_xlsx
            write_snapshot_xlsx(result, {"tool":source["tool"], "args":source["params"]}, path, include_title=False)
        rendered = XlsxPackage(path.read_bytes())
    sheets = rendered.meta.get("sheets", {}) if rendered.meta else {}
    if len(sheets) != 1 or not next(iter(sheets.values())).get("regions"):
        raise ConversionError("empty_result", "The fresh query has no renderable financial rows; no cells were replaced.")
    sheet, entry = next(iter(sheets.items()))
    region = entry["regions"][0]
    bounds = rendered.name_bounds(region["range_name"], sheet)
    return rendered, sheet, deepcopy(entry), bounds


def place_region(package, record, result, *, origin, source=None, prepared=None):
    sheet, anchor, layout = record["sheet"], record["anchor"], record["layout"]
    marker = record.get("_marker") or {}
    source = source or result.get("_workbook_source") or {"tool":record["_definition"].tool, "params":record["_definition"].params}
    validate_source(source)
    rendered, render_sheet, entry, size = prepared or render_region(result, source, layout)
    old = rectangle(record["bounds"])
    c, r, _, _ = rectangle(anchor)
    new = (c,r,c+size[2]-1,r+size[3]-1)
    rectangle(address(new))
    own_table = marker.get("table_name")
    package.check_placement(sheet, old, new, anchor=anchor if record["state"] == "live" else None, own_table=own_table)
    region = entry["regions"][0]
    region["origin"] = origin.rstrip("/")
    name = marker.get("range_name") or f"precis_region_{record['region_id']}"
    region["range_name"] = name
    region["placement"] = "bounded"
    footer_rows = region["shape"]["footer_rows"]
    data_bounds = (new[0], new[1], new[2], new[3] - footer_rows)
    package.replace_cells(sheet, old, rendered, render_sheet, new,
                          old_footer_rows=1 if marker else 0)
    package.set_name(name, sheet, address(new))
    if layout == "extract":
        table = package.place_table(sheet, rendered, render_sheet, data_bounds, name=own_table)
        region["table_name"] = table
        entry["excel_tables"] = [{"name":table, "ref":sheet_ref(sheet,address(data_bounds))}]
        named = []
        for index, column in enumerate(region["shape"]["value_columns"], start=len(region["shape"]["key_columns"])+1):
            previous_names = marker.get("named_ranges", []) if record["state"] == "static" else []
            previous = previous_names[index-len(region["shape"]["key_columns"])-1]["name"] if len(previous_names) > index-len(region["shape"]["key_columns"])-1 else None
            if previous:
                expected = (c+index-1,r+1,c+index-1,old[3]-1)
                if package.name_bounds(previous, sheet) != expected:
                    raise ConversionError("name_rebound", f"Generated value name {previous!r} no longer identifies its owned static column.")
            generated = next((n for n,col in record.get("_names", []) if col == index), None) or previous or f"ext_{record['region_id']}_{index}"
            ref = address((c+index-1, r+1, c+index-1, data_bounds[3]))
            package.set_name(generated, sheet, ref)
            named.append({"name":generated,"ref":sheet_ref(sheet,ref)})
        region["named_ranges"] = named
        entry["named_ranges"] = named
    for generated, column in record.get("_names", []):
        if column is not None:
            if layout != "extract" or not 1 <= column <= new[2]-c+1:
                raise ConversionError("unsupported_value_name", "A generated value name cannot be mapped onto this result.")
            package.set_name(generated, sheet, address((c+column-1,r+1,c+column-1,data_bounds[3])))
    # Shapes are region-relative; workbook names hold absolute placement.
    region["refresh"] = {key:deepcopy(entry[key]) for key in ("shape", "refresh_policy") if key in entry}
    if package.meta is None:
        package.meta = build_minimal_meta(workbook_kind=layout)
    metadata = package.meta.setdefault("sheets", {}).setdefault(sheet, {"kind":"user", "regions":[]})
    metadata["regions"] = [m for m in metadata.get("regions", []) if m.get("range_name") != name] + [region]
    metadata["kind"] = layout if len(metadata["regions"]) == 1 else "user"
    for key in ("source", "shape", "refresh_policy", "excel_tables", "named_ranges"):
        metadata.pop(key, None)
    package.meta["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"region_id":record["region_id"], "sheet":sheet, "anchor":anchor, "layout":layout, "action":"converted" if record["state"] == "live" else "updated", "bounds":address(new)}


def convert_regions(blob, *, origin, scope, tool_dispatch):
    if not origin:
        raise ConversionError("missing_server_origin", "PRECIS_BASE_URL must be configured before workbook conversion.")
    package, all_records = discover_regions(blob, origin=origin)
    selected = select_regions(all_records, scope, explicit=True)
    if not selected:
        raise ConversionError("empty_scope", "No supported regions were found in the requested scope.")
    blocked = [r for r in selected if "formula_to_static" not in r["eligible_directions"]]
    if blocked:
        return None, {"status":"error", "regions":public_regions(blocked), "code":"ineligible_scope", "message":"No output was published; the requested scope contains ineligible regions."}
    # All candidates must have disjoint authoritative old bounds before queries.
    for index, record in enumerate(selected):
        for other in all_records:
            if other is not record and other.get("bounds") and record["sheet"] == other["sheet"] and overlaps(rectangle(record["bounds"]), rectangle(other["bounds"])):
                raise ConversionError("overlapping_regions", "Saved region boundaries overlap; resolve them in Excel first.")
    outcomes = []
    for record in selected:
        try:
            definition = record["_definition"]
            result = tool_dispatch(definition.tool, definition.params)
            if not isinstance(result, dict) or result.get("error"):
                raise ConversionError("query_failed", "The governed source query was refused or failed: "+str(result.get("error", "No valid query result") if isinstance(result, dict) else "No valid query result"), "Check current access and the formula arguments, then retry.")
            rendered, render_sheet, entry, size = render_region(result, result.get("_workbook_source") or {"tool":definition.tool,"params":definition.params}, definition.layout)
            c,r,_,_ = rectangle(record["anchor"])
            new = (c,r,c+size[2]-1,r+size[3]-1)
            for other in all_records:
                if other is not record and other.get("bounds") and other["sheet"] == record["sheet"] and overlaps(new,rectangle(other["bounds"])):
                    raise ConversionError("growth_collision", "Fresh result growth overlaps another saved region.")
            outcomes.append(place_region(package, record, result, origin=origin, prepared=(rendered, render_sheet, entry, size)))
        except ConversionError as exc:
            return None, {"status":"error", "code":"conversion_refused", "regions":[{**public_regions([record])[0], "diagnostics":[exc.diagnostic()]}], "message":"No output was published; the input workbook is unchanged."}
    package.write_meta()
    package.recalculate_on_open()
    return package.bytes(), {"status":"ok", "regions":outcomes, "calculation":"deferred_to_excel", "message":"Fresh authorized queries replaced the selected formulas. Dependent calculations must run in Excel; original upload preserved."}
