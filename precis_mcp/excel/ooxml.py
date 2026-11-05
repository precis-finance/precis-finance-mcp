# SPDX-License-Identifier: Elastic-2.0
"""Bounded, targeted XLSX editing. Untouched ZIP payloads are copied verbatim.

openpyxl is used only to render a disposable new workbook. Uploaded workbooks
are parsed/edited as OOXML, retaining namespace declarations and extension parts.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
import posixpath
import re
import zipfile

from lxml import etree as ET
from openpyxl.utils.cell import get_column_letter, range_boundaries
import yaml

from precis_mcp.excel.formulas import ConversionError
from precis_mcp.excel.meta import META_SHEET_NAME, MetaError, _validate_meta

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
MAX_BYTES = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_CELLS = 250_000
MAX_REGIONS = 64


def tag(name):
    return f"{{{S}}}{name}"


def xml(raw: bytes):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = ET.fromstring(raw, parser)
        if root.getroottree().docinfo.doctype:
            raise ConversionError("unsafe_xml", "DTD declarations are not supported in uploaded workbooks.")
        return root
    except ET.XMLSyntaxError as exc:
        raise ConversionError("invalid_package", "The workbook contains malformed XML.") from exc


def rectangle(ref: str) -> tuple[int, int, int, int]:
    if not isinstance(ref, str) or not re.fullmatch(r"\$?[A-Za-z]{1,3}\$?[1-9]\d*(?::\$?[A-Za-z]{1,3}\$?[1-9]\d*)?", ref):
        raise ConversionError("invalid_bounds", "Region bounds must be one ordinary rectangular cell range.")
    c1, r1, c2, r2 = range_boundaries(ref)
    if c2 > 16384 or r2 > 1048576 or c2 < c1 or r2 < r1 or (c2-c1+1)*(r2-r1+1) > MAX_CELLS:
        raise ConversionError("region_limit", "Region exceeds the supported dimensions or cell limit.")
    return c1, r1, c2, r2


def address(bounds):
    c1, r1, c2, r2 = bounds
    return f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"


def contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def sheet_ref(sheet, ref):
    return "'" + sheet.replace("'", "''") + "'!" + ref


def _style_key(element):
    """Namespace declarations/comments do not change a style's identity."""
    return (element.tag, tuple(sorted(element.attrib.items())), element.text or "",
            tuple(_style_key(child) for child in element if isinstance(child.tag,str)))


class XlsxPackage:
    """In-memory bounded package; no external relationship is ever fetched."""

    def __init__(self, blob: bytes):
        if len(blob) > MAX_BYTES:
            raise ConversionError("file_limit", "Workbook exceeds the 32 MiB conversion limit.")
        try:
            with zipfile.ZipFile(BytesIO(blob)) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if len(infos) > 4096 or len(set(names)) != len(names) or sum(i.file_size for i in infos) > MAX_EXPANDED:
                    raise ConversionError("package_limit", "Workbook has duplicate parts or exceeds package limits.")
                if any(i.flag_bits & 1 or i.file_size > MAX_BYTES for i in infos):
                    raise ConversionError("package_limit", "Encrypted or oversized workbook parts are unsupported.")
                if any(n.startswith("/") or "\\" in n or ".." in n.split("/") for n in names):
                    raise ConversionError("invalid_package", "Workbook contains invalid package part names.")
                self.parts = {i.filename: archive.read(i) for i in infos}
                self.infos = {i.filename: i for i in infos}
        except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
            raise ConversionError("invalid_package", "Upload an unencrypted .xlsx workbook.") from exc
        if any(n.startswith("_xmlsignatures/") or "vbaProject" in n for n in self.parts):
            raise ConversionError("unsupported_container", "Signed or macro-enabled workbooks cannot be converted.")
        self.dirty = {}
        self.trees = {}
        self.workbook = self.tree("xl/workbook.xml")
        if self.workbook.tag != tag("workbook"):
            raise ConversionError("unsupported_container", "Only transitional .xlsx workbooks are supported.")
        self.rels = self.tree("xl/_rels/workbook.xml.rels")
        targets = {e.get("Id"): self.target("xl/workbook.xml", e.get("Target", "")) for e in self.rels if e.get("TargetMode") != "External"}
        self.sheets = {}
        for e in self.workbook.findall(f"{tag('sheets')}/{tag('sheet')}"):
            part = targets.get(e.get(f"{{{R}}}id"))
            if not part or part not in self.parts or e.get("name") in self.sheets:
                raise ConversionError("invalid_package", "Workbook sheet relationships are invalid.")
            self.sheets[e.get("name")] = part
        if len(self.sheets) > 256:
            raise ConversionError("package_limit", "Workbook has too many sheets.")
        self.shared = []
        if "xl/sharedStrings.xml" in self.parts:
            self.shared = ["".join(t.text or "" for t in e.iter(tag("t"))) for e in self.tree("xl/sharedStrings.xml").findall(tag("si"))]
        self.meta = self._read_meta()

    @staticmethod
    def target(part, value):
        resolved = posixpath.normpath(value.lstrip("/") if value.startswith("/") else posixpath.join(posixpath.dirname(part), value))
        if resolved.startswith("../") or ":" in resolved:
            raise ConversionError("invalid_package", "Workbook has an invalid internal relationship.")
        return resolved

    def tree(self, part):
        if part not in self.trees:
            if part not in self.parts:
                raise ConversionError("invalid_package", "Workbook is missing a required OOXML part.")
            self.trees[part] = xml(self.parts[part])
        return self.trees[part]

    def changed(self, part):
        self.dirty[part] = self.tree(part)

    def sheet(self, name):
        if name not in self.sheets:
            raise ConversionError("unknown_sheet", f"Sheet {name!r} does not exist.")
        return self.tree(self.sheets[name])

    def cells(self, sheet):
        return {cell.get("r"): cell for cell in self.sheet(sheet).iter(tag("c"))}

    def tables_for_sheet(self, sheet):
        part = self.sheets[sheet]
        rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part)+".rels")
        if rel_part not in self.parts:
            return []
        used = {node.get(f"{{{R}}}id") for node in self.sheet(sheet).findall(f"{tag('tableParts')}/{tag('tablePart')}")}
        return [(self.target(part,rel.get("Target","")),self.tree(self.target(part,rel.get("Target",""))))
                for rel in self.tree(rel_part) if rel.get("Id") in used and rel.get("Type", "").endswith("/table") and rel.get("TargetMode") != "External"]

    def value(self, cell):
        if cell is None:
            return None
        kind, v = cell.get("t"), cell.find(tag("v"))
        if kind == "inlineStr":
            return "".join(e.text or "" for e in cell.iter(tag("t")))
        text = v.text if v is not None else None
        if kind == "s":
            try:
                return self.shared[int(text)]
            except (ValueError, TypeError, IndexError) as exc:
                raise ConversionError("invalid_package", "Workbook has an invalid shared string reference.") from exc
        if text is None:
            return None
        if kind in {"str", "e", "d"}:
            return text
        if kind == "b":
            return text == "1"
        try:
            return float(text)
        except ValueError as exc:
            raise ConversionError("invalid_package", "Workbook contains an invalid numeric cell.") from exc

    def names(self):
        return list(self.workbook.findall(f"{tag('definedNames')}/{tag('definedName')}"))

    def resolve_scalar(self, sheet, expression):
        """One scalar cell, optionally sheet-qualified or through a defined name."""
        def resolve(text, seen):
            match = re.fullmatch(r"(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?(\$?[A-Za-z]{1,3}\$?[1-9]\d*)", text)
            if match:
                target_sheet = (match[1].replace("''", "'") if match[1] else match[2]) or sheet
                target_sheet = next((name for name in self.sheets if name.casefold() == target_sheet.casefold()), target_sheet)
                coordinate = match[3].replace("$", "").upper()
                rectangle(coordinate)
                cell = self.cells(target_sheet).get(coordinate)
                for candidate in self.cells(target_sheet).values():
                    formula = candidate.find(tag("f"))
                    if formula is not None and formula.get("ref") and contains(rectangle(formula.get("ref")), rectangle(coordinate)):
                        raise ConversionError("formula_argument", f"Argument {text!r} is inside a formula-produced array range; cached values cannot establish its current value.", "Use a literal or a non-formula scalar argument cell, then upload again.")
                if cell is not None and (cell.find(tag("f")) is not None or cell.get("t") == "e"):
                    raise ConversionError("formula_argument", f"Argument {text!r} is a formula or error cell; cached values cannot establish its current value.", "Replace that argument with a literal or reference to a non-formula scalar cell, then upload again.")
                return self.value(cell)
            if not re.fullmatch(r"[A-Za-z_\\][A-Za-z0-9_.\\]*", text) or text.lower() in seen:
                raise ConversionError("unsupported_reference", f"Argument {text!r} is not a supported scalar reference or name.")
            candidates = [n for n in self.names() if n.get("name", "").lower() == text.lower()]
            sheet_index = list(self.sheets).index(sheet)
            local = [n for n in candidates if n.get("localSheetId") == str(sheet_index)]
            candidates = local or [n for n in candidates if n.get("localSheetId") is None]
            if len(candidates) != 1:
                raise ConversionError("unresolved_name", f"Cannot resolve scalar name {text!r}.")
            return resolve((candidates[0].text or "").lstrip("="), seen | {text.lower()})
        return resolve(expression, set())

    def _read_meta(self):
        if META_SHEET_NAME not in self.sheets:
            return None
        raw = self.value(self.cells(META_SHEET_NAME).get("A1"))
        if not isinstance(raw, str) or len(raw) > 32767:
            raise ConversionError("invalid_metadata", "Précis metadata is empty or too large.")
        try:
            meta = yaml.safe_load(raw)
            # Bound alias expansion before JSON serialization (including cycles).
            nodes, chars = 0, 0
            ancestors = set()
            def check(value, depth=0):
                nonlocal nodes, chars
                nodes += 1
                if nodes > 10000 or depth > 50:
                    raise ValueError("Metadata expansion limit")
                if isinstance(value, (dict, list)):
                    if id(value) in ancestors:
                        raise ValueError("Recursive metadata")
                    ancestors.add(id(value))
                    for key, child in value.items() if isinstance(value, dict) else enumerate(value):
                        chars += len(str(key))
                        check(child, depth+1)
                    ancestors.remove(id(value))
                else:
                    chars += len(str(value))
                if chars > 32767:
                    raise ValueError("Metadata expansion limit")
            check(meta)
            if len(json.dumps(meta, allow_nan=False)) > 32767:
                raise ValueError("Expanded metadata exceeds limit")
            if not isinstance(meta, dict):
                raise ValueError("Metadata must be an object")
            _validate_meta(meta)
            return meta
        except (ValueError, TypeError, RecursionError, yaml.YAMLError, MetaError) as exc:
            raise ConversionError("invalid_metadata", "Précis metadata is malformed; regenerate the export.") from exc

    def name_bounds(self, name, sheet):
        names = [e for e in self.names() if e.get("name") == name and e.get("localSheetId") is None]
        if len(names) != 1:
            raise ConversionError("missing_bounds", "Region boundary name is missing or ambiguous.")
        pattern = r"^=?'" + re.escape(sheet.replace("'", "''")) + r"'!(.+)$"
        match = re.fullmatch(pattern, names[0].text or "")
        if not match:
            match = re.fullmatch(r"^=?" + re.escape(sheet) + r"!(.+)$", names[0].text or "")
        if not match or match[1].endswith("#"):
            raise ConversionError("missing_bounds", "Region name does not provide a fixed rectangle; saved array/spill bounds are required.")
        return rectangle(match[1])

    def check_placement(self, sheet, old, new, *, anchor=None, own_table=None):
        root = self.sheet(sheet)
        if root.find(tag("sheetProtection")) is not None:
            raise ConversionError("protected_sheet", f"Sheet {sheet!r} is protected.")
        if any(root.find(tag(n)) is not None for n in ("drawing", "legacyDrawing", "oleObjects", "pivotTableParts")):
            raise ConversionError("unsupported_sheet_feature", f"Sheet {sheet!r} has drawings, objects or pivots whose placement cannot be validated.", "Move the selected region to a sheet without those objects, then upload again.")
        cells = self.cells(sheet)
        for coord, cell in cells.items():
            bounds = rectangle(coord)
            formula = cell.find(tag("f"))
            if formula is not None and formula.get("ref") and coord != anchor and overlaps(rectangle(formula.get("ref")), new):
                raise ConversionError("array_collision", "Another array/spill range intersects the replacement.")
            if not overlaps(bounds, new) and not overlaps(bounds, old):
                continue
            if formula is not None and coord != anchor:
                raise ConversionError("region_formula", f"Another formula at {sheet}!{coord} intersects the replacement.")
            if not contains(old, bounds) and (formula is not None or self.value(cell) is not None or cell.get("cm") or cell.get("vm")):
                raise ConversionError("growth_collision", f"The new result would overwrite {sheet}!{coord}.", "Clear or move the neighbouring cells in Excel, or request a separate export.")
            if cell.get("vm") or cell.find(tag("extLst")) is not None:
                raise ConversionError("unsupported_cell_feature", f"Cell {sheet}!{coord} contains unsupported value metadata or extensions.")
        for merge in root.findall(f"{tag('mergeCells')}/{tag('mergeCell')}"):
            bounds = rectangle(merge.get("ref"))
            if overlaps(bounds, old) or overlaps(bounds, new):
                if not contains(old, bounds):
                    raise ConversionError("merge_collision", "A merged range crosses the owned region boundary.")
        for local, attr in [("hyperlink", "ref"), ("dataValidation", "sqref"), ("conditionalFormatting", "sqref")]:
            for element in root.iter(tag(local)):
                for ref in element.get(attr, "").split():
                    if overlaps(rectangle(ref), old) or overlaps(rectangle(ref), new):
                        raise ConversionError("unsupported_region_feature", f"Selected cells contain {local} rules or links.")
        for part, table in self.tables_for_sheet(sheet):
            if overlaps(rectangle(table.get("ref")), old) or overlaps(rectangle(table.get("ref")), new):
                if table.get("displayName") != own_table:
                    raise ConversionError("table_collision", "An Excel Table intersects the replacement.")

    def set_name(self, name, sheet, ref):
        container = self.workbook.find(tag("definedNames"))
        if container is None:
            container = ET.Element(tag("definedNames"))
            self._insert(self.workbook, container, {"calcPr", "oleSize", "customWorkbookViews", "pivotCaches", "smartTagPr", "webPublishing", "fileRecoveryPr", "webPublishObjects", "extLst"})
        matches = [n for n in container if n.get("name", "").lower() == name.lower()]
        if len(matches) > 1 or matches and matches[0].get("localSheetId") is not None:
            raise ConversionError("name_collision", f"Defined name {name!r} is ambiguous.")
        node = matches[0] if matches else ET.SubElement(container, tag("definedName"), name=name)
        node.text = sheet_ref(sheet, ref)
        self.changed("xl/workbook.xml")

    @staticmethod
    def _insert(parent, element, following):
        for index, child in enumerate(parent):
            if isinstance(child.tag,str) and ET.QName(child).localname in following:
                parent.insert(index, element)
                return
        parent.append(element)

    def _styles(self, rendered):
        """Append/remap generated styles; existing style indexes never change."""
        target, source = self.tree("xl/styles.xml"), rendered.tree("xl/styles.xml")
        maps = {}
        for section in ("numFmts", "fonts", "fills", "borders", "cellStyleXfs", "cellXfs"):
            src = source.find(tag(section))
            if src is None:
                continue
            dest = target.find(tag(section))
            if dest is None:
                dest = ET.Element(tag(section), count="0")
                order = ["numFmts", "fonts", "fills", "borders", "cellStyleXfs", "cellXfs", "cellStyles", "dxfs", "tableStyles", "colors", "extLst"]
                self._insert(target, dest, set(order[order.index(section)+1:]))
            mapping = {}
            for index, original in enumerate(src):
                node = deepcopy(original)
                key = int(node.get("numFmtId")) if section == "numFmts" else index
                if section in {"cellStyleXfs", "cellXfs"}:
                    for attr, family in [("fontId", "fonts"), ("fillId", "fills"), ("borderId", "borders"), ("xfId", "cellStyleXfs"), ("numFmtId", "numFmts")]:
                        old = int(node.get(attr, "0"))
                        if old in maps.get(family, {}):
                            node.set(attr, str(maps[family][old]))
                if section == "numFmts":
                    existing = next((e for e in dest if e.get("formatCode") == node.get("formatCode")), None)
                    number = int(existing.get("numFmtId")) if existing is not None else max([163] + [int(e.get("numFmtId")) for e in dest]) + 1
                    node.set("numFmtId", str(number))
                else:
                    nodes = [e for e in dest if isinstance(e.tag,str)]
                    existing = next((i for i, e in enumerate(nodes) if _style_key(e) == _style_key(node)), None)
                    number = existing if existing is not None else len(nodes)
                mapping[key] = number
                if section == "numFmts" and not any(e.get("numFmtId") == node.get("numFmtId") for e in dest) or section != "numFmts" and existing is None:
                    if len(dest) >= 64000:
                        raise ConversionError("style_limit", "Workbook exceeds the supported style count.")
                    dest.append(node)
            dest.set("count", str(sum(isinstance(e.tag,str) for e in dest)))
            maps[section] = mapping
        self.changed("xl/styles.xml")
        return maps["cellXfs"]

    def replace_cells(self, sheet, old, rendered, render_sheet, new, *, old_footer_rows=0):
        root, src = self.sheet(sheet), rendered.sheet(render_sheet)
        styles = self._styles(rendered)
        data = root.find(tag("sheetData"))
        if data is None:
            raise ConversionError("invalid_package", "Worksheet is missing sheetData.")
        for row in data:
            for cell in list(row):
                if cell.tag == tag("c") and contains(old, rectangle(cell.get("r"))):
                    row.remove(cell)
        rows = {int(row.get("r")): row for row in data}
        footers = [new] + ([old] if old_footer_rows == 1 else [])
        for left, _, right, bottom in footers:
            row = rows.get(bottom)
            # Unwrapped footers need no custom height. Preserve shared rows.
            if row is not None and not any(cell.tag == tag("c") and not left <= rectangle(cell.get("r"))[0] <= right for cell in row):
                row.attrib.pop("ht", None)
                row.attrib.pop("customHeight", None)
        dc, dr = new[0]-1, new[1]-1
        for src_row in src.findall(f"{tag('sheetData')}/{tag('row')}"):
            for original in src_row.findall(tag("c")):
                node = deepcopy(original)
                c, r, _, _ = rectangle(node.get("r"))
                r, c = r+dr, c+dc
                node.set("r", f"{get_column_letter(c)}{r}")
                node.set("s", str(styles[int(node.get("s", "0"))]))
                # Generated snapshots never carry executable formulas, including
                # analytical text beginning with '='. Do not copy metadata indexes.
                if node.find(tag("f")) is not None:
                    raise ConversionError("unsafe_render", "Static renderer returned an executable formula.")
                for attr in ("cm", "vm"):
                    node.attrib.pop(attr, None)
                if r not in rows:
                    row = ET.Element(tag("row"), r=str(r))
                    data.append(row)
                    rows[r] = row
                row = rows[r]
                row.append(node)
        for row in rows.values():
            row[:] = sorted(row, key=lambda cell:rectangle(cell.get("r"))[0] if cell.tag == tag("c") else 16385)
        data[:] = sorted(data,key=lambda row:int(row.get("r")))
        merges = root.find(tag("mergeCells"))
        if merges is not None:
            for merge in list(merges):
                if contains(old, rectangle(merge.get("ref"))):
                    merges.remove(merge)
        for original in src.findall(f"{tag('mergeCells')}/{tag('mergeCell')}"):
            if merges is None:
                merges = ET.Element(tag("mergeCells"))
                self._insert(root, merges, {"phoneticPr", "conditionalFormatting", "dataValidations", "hyperlinks", "printOptions", "pageMargins", "pageSetup", "headerFooter", "drawing", "tableParts", "extLst"})
            c1, r1, c2, r2 = rectangle(original.get("ref"))
            ET.SubElement(merges, tag("mergeCell"), ref=address((c1+dc,r1+dr,c2+dc,r2+dr)))
        if merges is not None:
            if len(merges):
                merges.set("count", str(len(merges)))
            else:
                root.remove(merges)
        dimension = root.find(tag("dimension"))
        if dimension is not None:
            existing = rectangle(dimension.get("ref", "A1"))
            dimension.set("ref", address((min(existing[0],new[0]), min(existing[1],new[1]), max(existing[2],new[2]), max(existing[3],new[3]))))
        self.changed(self.sheets[sheet])

    def add_relationship(self, part, rel_type, target):
        rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if rel_part not in self.parts:
            self.parts[rel_part] = b""
            self.trees[rel_part] = ET.Element(f"{{{P}}}Relationships", nsmap={None:P})
        root = self.tree(rel_part)
        used = {e.get("Id") for e in root}
        index = 1
        while f"rId{index}" in used:
            index += 1
        rid = f"rId{index}"
        ET.SubElement(root, f"{{{P}}}Relationship", Id=rid, Type=f"{R}/{rel_type}", Target=target)
        self.changed(rel_part)
        return rid

    def content_type(self, part, kind):
        root = self.tree("[Content_Types].xml")
        if not any(e.get("PartName") == "/"+part for e in root):
            ET.SubElement(root, f"{{{CT}}}Override", PartName="/"+part, ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml")
            self.changed("[Content_Types].xml")

    def place_table(self, sheet, rendered, render_sheet, new, *, name=None):
        """Integrate one shared-renderer Table, retaining its identity on refresh."""
        tables = [(part, self.tree(part)) for part in self.parts if part.startswith("xl/tables/") and part.endswith(".xml")]
        matches = [(part, tree) for part, tree in tables if tree.get("displayName") == name] if name else []
        if name and len(matches) != 1:
            raise ConversionError("missing_table", "The static region's Excel Table is missing or ambiguous.")
        donor_part = next(p for p in rendered.parts if p.startswith("xl/tables/") and p.endswith(".xml"))
        donor = deepcopy(rendered.tree(donor_part))
        if matches:
            part, existing = matches[0]
            if any(existing.find(tag(key)) is not None for key in ("extLst","sortState")) or existing.get("totalsRowCount", "0") != "0" or existing.get("headerRowCount","1") != "1" or existing.get("connectionId") or existing.get("tableType","worksheet") != "worksheet" or any(existing.iter(tag("calculatedColumnFormula"))) or any(existing.iter(tag("totalsRowFormula"))):
                raise ConversionError("unsupported_table", "The region's Table has unsupported extensions or totals.")
            if [c.get("name") for c in existing.findall(f"{tag('tableColumns')}/{tag('tableColumn')}")] != [c.get("name") for c in donor.findall(f"{tag('tableColumns')}/{tag('tableColumn')}")]:
                raise ConversionError("table_columns_changed", "Refreshing Table column definitions is not supported.")
            existing.set("ref", address(new))
            auto = existing.find(tag("autoFilter"))
            if auto is not None:
                auto.set("ref", address(new))
            self.changed(part)
            return name
        used = {t.get("displayName", "").lower() for _, t in tables} | {n.get("name", "").lower() for n in self.names()}
        index = 1
        while f"PrecisExtract{index}".lower() in used or f"xl/tables/precis{index}.xml" in self.parts:
            index += 1
        name, part = f"PrecisExtract{index}", f"xl/tables/precis{index}.xml"
        donor.attrib.update({"name":name, "displayName":name, "id":str(max([int(t.get('id')) for _,t in tables]+[0])+1), "ref":address(new)})
        auto = donor.find(tag("autoFilter"))
        if auto is not None:
            auto.set("ref", address(new))
        self.parts[part] = b""
        self.trees[part] = donor
        self.changed(part)
        sheet_part = self.sheets[sheet]
        rid = self.add_relationship(sheet_part, "table", posixpath.relpath(part, posixpath.dirname(sheet_part)))
        root = self.sheet(sheet)
        table_parts = root.find(tag("tableParts"))
        if table_parts is None:
            table_parts = ET.Element(tag("tableParts"))
            self._insert(root, table_parts, {"extLst"})
        ET.SubElement(table_parts, tag("tablePart"), attrib={f"{{{R}}}id":rid})
        table_parts.set("count", str(len(table_parts)))
        self.changed(sheet_part)
        self.content_type(part, "table")
        return name

    def write_meta(self):
        _validate_meta(self.meta)
        raw = json.dumps(self.meta, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-16-le"))//2 > 32767:
            raise ConversionError("metadata_limit", "Converted provenance exceeds Excel's cell limit; reduce the requested scope.")
        if META_SHEET_NAME not in self.sheets:
            index = 1
            while f"xl/worksheets/precisMeta{index}.xml" in self.parts:
                index += 1
            part = f"xl/worksheets/precisMeta{index}.xml"
            self.parts[part] = b""
            self.trees[part] = ET.Element(tag("worksheet"), nsmap={None:S})
            ET.SubElement(self.trees[part], tag("sheetData"))
            rid = self.add_relationship("xl/workbook.xml", "worksheet", posixpath.relpath(part, "xl"))
            sheets = self.workbook.find(tag("sheets"))
            sid = max([int(e.get("sheetId")) for e in sheets] + [0])+1
            ET.SubElement(sheets, tag("sheet"), name=META_SHEET_NAME, sheetId=str(sid), state="hidden", attrib={f"{{{R}}}id":rid})
            self.sheets[META_SHEET_NAME] = part
            self.content_type(part, "worksheet")
            self.changed("xl/workbook.xml")
        root = self.sheet(META_SHEET_NAME)
        data = root.find(tag("sheetData"))
        data.clear()
        row = ET.SubElement(data, tag("row"), r="1")
        cell = ET.SubElement(row, tag("c"), r="A1", t="inlineStr")
        ET.SubElement(ET.SubElement(cell, tag("is")), tag("t")).text = raw
        self.changed(self.sheets[META_SHEET_NAME])

    def recalculate_on_open(self):
        calc = self.workbook.find(tag("calcPr"))
        if calc is None:
            calc = ET.Element(tag("calcPr"))
            self._insert(self.workbook, calc, {"oleSize", "customWorkbookViews", "pivotCaches", "smartTagPr", "webPublishing", "fileRecoveryPr", "webPublishObjects", "extLst"})
        calc.attrib.update({"calcMode":"auto", "fullCalcOnLoad":"1", "forceFullCalc":"1"})
        # The dependency chain describes the old formula graph. Excel rebuilds
        # it; unrelated formula caches remain untouched and are explicitly stale.
        if "xl/calcChain.xml" in self.parts:
            del self.parts["xl/calcChain.xml"]
            for rel in list(self.rels):
                if rel.get("Type", "").endswith("/calcChain"):
                    self.rels.remove(rel)
            ct = self.tree("[Content_Types].xml")
            for element in list(ct):
                if element.get("PartName") == "/xl/calcChain.xml":
                    ct.remove(element)
            self.changed("xl/_rels/workbook.xml.rels")
            self.changed("[Content_Types].xml")
        self.changed("xl/workbook.xml")

    def bytes(self):
        output = BytesIO()
        expanded = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in self.parts.items():
                if name in self.dirty:
                    raw = ET.tostring(self.dirty[name], encoding="UTF-8", xml_declaration=True)
                expanded += len(raw)
                if len(raw) > MAX_BYTES or expanded > MAX_EXPANDED:
                    raise ConversionError("package_limit", "Converted package exceeds supported part/expanded-size limits.")
                archive.writestr(self.infos.get(name, name), raw)
        blob = output.getvalue()
        if len(blob) > MAX_BYTES:
            raise ConversionError("file_limit", "Converted workbook exceeds the output size limit.")
        return blob
