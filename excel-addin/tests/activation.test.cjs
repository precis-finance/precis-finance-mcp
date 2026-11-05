const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const ts = require("typescript");
require.extensions[".ts"] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 }, fileName: filename,
}).outputText, filename);
const config = require("../src/config.ts");
const { activateSelectedExtract } = require("../src/regions/activate.ts");
global.CustomFunctions = { associate() {} };
const { metric, statement } = require("../src/functions/functions.ts");

// A bounded Office adapter fake: checks workbook outcomes and failure recovery.
// It supplies no evidence about real Office execution or host-specific APIs.
function fixture(mode = "ok", layout = "extract", tool = "run_metric", grouped = true) {
  global.window = { location: { href: "https://finance.example/excel/taskpane.html" } };
  global.Office = { context: { requirements: { isSetSupported: () => true } } };
  const projection = { version: 1, grid: [["department", "Actuals"], ["D1", 123.456789], ["D2", 456.789123]],
    number_formats: [["General", "General"], ["General", "#,##0.00"], ["General", "#,##0.00"]], key_columns: ["department"], value_columns: ["Actuals"], header_rows: 1 };
  const params = { metrics: ["revenue"], period_start: "2026-01", period_end: "2026-03",
    scenarios: [{ scenario: "actuals", alias: "Actuals" }], dimensions: ["department"], scale: 0, decimals: 2 };
  if (tool === "run_statement") { delete params.metrics; params.statement = "pnl"; }
  const report = layout === "report";
  const block = { type: "financial_table", columns: [{ key: "label", label: "Department" },
    { key: "Actuals", label: "Actuals", group: "Revenue" }, { key: "Var", label: "Var", group: "Revenue", format: "percent" }],
    rows: [{ row_type: "group_header", label: "Group", values: {} },
      { row_type: "line_item", label: "D1", values: { Actuals: 123.456789, Var: 8.7654321 }, indent: 1, cell_nf: { Actuals: "#,##0.00", Var: '0.00"%"' } },
      { row_type: "separator" }, { row_type: "total", label: "Total", values: { Actuals: 123.456789, Var: 8.7654321 }, cell_nf: { Actuals: "#,##0.00", Var: '0.00"%"' } }] };
  const reportProjection = { version: 1, grid: [["", "Revenue", ""], ["Department", "Actuals", "Var"], ["Group", "", ""],
    ["D1", 123.456789, 8.7654321], ["Total", 123.456789, 8.7654321]], header_rows: 2, column_keys: ["label", "Actuals", "Var"],
    number_formats: [["General", "General", "General"], ["General", "General", "General"], ["General", "General", "General"],
      ["General", "#,##0.00", '0.00"%"'], ["General", "#,##0.00", '0.00"%"']] };
  projection.report = reportProjection;
  if (!grouped) {
    for (const column of block.columns) delete column.group;
    reportProjection.grid.shift(); reportProjection.number_formats.shift(); reportProjection.header_rows = 1;
  }
  for (const p of [projection, reportProjection]) {
    p.footer_rows = 1;
    p.data_rows = p.grid.length - (p.header_rows || 1);
    p.grid.push(["Actuals · 2026-01 to 2026-03 · Data as at now", ...p.grid[0].slice(1).map(() => "")]);
    p.number_formats.push(p.grid[0].map(() => "General"));
  }
  const meta = { precis_meta_version: 1, workbook_kind: "extract", sheets: { Extract: { kind: "extract",
    regions: [{ range_name: "region", state: "static", layout: "extract", origin: "https://finance.example", table_name: "tbl_extract",
      source: { tool: "run_metric", params }, presentation: { scale: 0, decimals: 2 },
      shape: { key_columns: ["department"], value_columns: ["Actuals"], footer_rows: 1 }, named_ranges: [{ name: "ext_actuals" }] }] } } };
  const region = meta.sheets.Extract.regions[0];
  region.source.tool = tool;
  if (report) {
    meta.workbook_kind = meta.sheets.Extract.kind = "report";
    region.layout = "report"; region.shape = { column_keys: reportProjection.column_keys, header_row: 1, data_start_row: grouped ? 3 : 2, row_count: 4, footer_rows: 1 };
    delete region.named_ranges; delete region.table_name;
  }
  const sheets = new Map(), names = new Map();
  let fetches = 0, pending, calculationCalls = 0, commitFailed = false;
  const letter = c => String.fromCharCode(65 + c);
  class Range {
    constructor(sheet, r, c, h, w, absent = false) { this.worksheet = sheet; this.rowIndex = r; this.columnIndex = c; this.rowCount = h; this.columnCount = w; this.isNullObject = absent; }
    load() { return this; }
    get address() { return `${this.worksheet.name}!${letter(this.columnIndex)}${this.rowIndex + 1}${this.rowCount * this.columnCount > 1 ? `:${letter(this.columnIndex + this.columnCount - 1)}${this.rowIndex + this.rowCount}` : ""}`; }
    each(fn) { for (let r = 0; r < this.rowCount; r++) for (let c = 0; c < this.columnCount; c++) fn(this.worksheet.cell(this.rowIndex + r, this.columnIndex + c), r, c); }
    matrix(fn) { return Array.from({ length: this.rowCount }, (_, r) => Array.from({ length: this.columnCount }, (_, c) => fn(this.worksheet.cell(this.rowIndex + r, this.columnIndex + c)))); }
    get values() { return this.matrix(c => c.value); }
    set values(value) { this.each((c, r, col) => { c.value = value[r][col]; c.formula = undefined; }); }
    get formulas() { return this.matrix(c => c.formula ?? c.value); }
    set formulas(value) { this.each((c, r, col) => { c.formula = value[r][col]; c.value = "#BUSY!"; }); }
    get numberFormat() { return this.matrix(c => c.format); }
    set numberFormat(value) { this.each((c, r, col) => c.format = value[r][col]); }
    get hasSpill() { return !!this.worksheet.spill; }
    getCell(r, c) { return new Range(this.worksheet, this.rowIndex + r, this.columnIndex + c, 1, 1); }
    getColumn(c) { return new Range(this.worksheet, this.rowIndex, this.columnIndex + c, this.rowCount, 1); }
    getRow(r) { return new Range(this.worksheet, this.rowIndex + r, this.columnIndex, 1, this.columnCount); }
    getEntireRow() { return new Range(this.worksheet,this.rowIndex,0,this.rowCount,16384); }
    get format() {
      const cell = this.worksheet.cell(this.rowIndex, this.columnIndex);
      if (!cell.visual) {
        const sheet=this.worksheet,row=this.rowIndex;
        cell.visual = { fill: {}, font: {}, borders: { getItem: () => ({}) }, autofitRows() {}, load() { return this; }, columnWidth: 80 };
        Object.defineProperties(cell.visual, {
          rowHeight: { get: () => sheet.rowHeights.get(row) ?? 15, set: height => sheet.rowHeights.set(row,height) },
          useStandardHeight: { get: () => !sheet.rowHeights.has(row), set: standard => { if (standard) sheet.rowHeights.delete(row); } },
        });
      }
      return cell.visual;
    }
    getSpecialCellsOrNullObject() { return { isNullObject: !this.formulas.flat().some(v => typeof v === "string" && v.startsWith("=")), load() {} }; }
    getMergedAreasOrNullObject() {
      const { overlaps } = require("../src/regions/contract.ts");
      const box = r => ({ row: r.rowIndex, column: r.columnIndex, rows: r.rowCount, columns: r.columnCount });
      const items = this.worksheet.merges.filter(m => overlaps(box(m), box(this)));
      return { isNullObject: !items.length, load() {}, areas: { items, load() {} } };
    }
    merge() { this.worksheet.merges.push(this); }
    unmerge() {
      this.worksheet.merges = this.worksheet.merges.filter(m => !(m.rowIndex >= this.rowIndex && m.rowIndex < this.rowIndex + this.rowCount && m.columnIndex >= this.columnIndex && m.columnIndex < this.columnIndex + this.columnCount));
    }
    copyFrom(other, type) {
      if (mode === "restore-fail" && other.worksheet.name.startsWith("_PrecisRecovery_")) throw new Error("injected restore failure");
      const values = type === "formats" ? other.numberFormat : other.values;
      if (type === "formats") this.numberFormat = values; else this.values = values;
      if (type === "all") {
        this.numberFormat = other.numberFormat;
        this.unmerge();
        for (const m of other.getMergedAreasOrNullObject().areas.items) {
          this.worksheet.getRangeByIndexes(this.rowIndex + m.rowIndex - other.rowIndex, this.columnIndex + m.columnIndex - other.columnIndex, m.rowCount, m.columnCount).merge();
        }
      }
      if (mode === "preflight-edit" && this.worksheet.name.startsWith("_PrecisRecovery_")) extract.cell(1, 0).value = "user edit";
    }
    clear(type) {
      if (type === "Formats") { this.each(c => c.format = "General"); return; }
      if (this.rowCount === 1 && this.columnCount === 1 && this.worksheet.spill) {
        this.worksheet.spill.each(c => { c.value = ""; c.formula = undefined; }); this.worksheet.spill = undefined;
      }
      this.each(c => { c.value = ""; c.formula = undefined; if (type === "All") c.format = "General"; });
    }
    getSpillingToRangeOrNullObject() { return this.worksheet.spill || new Range(this.worksheet, 0, 0, 0, 0, true); }
    calculate() {
      calculationCalls++;
      pending = async () => {
        if (mode === "fail-caption-edit") this.worksheet.cell(0, 0).value = "user caption edit";
        if (["fail", "restore-fail", "rollback-cleanup-fail", "fail-caption-edit"].includes(mode)) throw new Error("injected calculation failure");
        if (mode === "growth-edit") { this.worksheet.getRangeByIndexes(2, 1, 1, 1).values = [["user edit"]]; return; }
        const formula = this.formulas[0][0];
        const grid = tool === "run_statement" ? await statement("pnl", "2026-01", "2026-03", "actuals as Actuals", "department", "", 0, 2, layout, { address: this.address }) :
          await metric("revenue", "2026-01", "2026-03", "department", "actuals as Actuals", "", layout, 0, 2, { address: this.address });
        const spill = this.worksheet.getRangeByIndexes(this.rowIndex, this.columnIndex, grid.length, grid[0].length);
        spill.values = grid;
        this.worksheet.cell(this.rowIndex, this.columnIndex).formula = formula;
        this.worksheet.spill = spill;
        if (mode === "session-edit") config.setToken("replacement-session");
        if (mode === "caption-edit") this.worksheet.cell(0, 0).value = "user caption edit";
        if (mode === "metadata-edit") {
          const changed = JSON.parse(metadata.cell(0, 0).value);
          changed.sheets.Unrelated = { kind: "user", note: "concurrent provenance edit" };
          metadata.cell(0, 0).value = JSON.stringify(changed);
        }
        if (mode === "merge-edit") { this.worksheet.getRangeByIndexes(7, 1, 1, 2).merge(); throw new Error("competing merge"); }
      };
    }
  }
  class Sheet {
    constructor(name) {
      this.name = this.id = name; this.cells = new Map(); this.protection = { protected: false, load() {} };
      this.rowHeights = new Map();
      this.slicers = { items: [], load() {} }; this.tableItems = []; this.merges = [];
      this.tables = { load() {}, get items() { return this.owner.tableItems; }, owner: this,
        add: (range) => { const t = this.makeTable("restored", range); this.tableItems.push(t); return t; } };
    }
    load() { return this; }
    cell(r, c) { const key = `${r},${c}`; if (!this.cells.has(key)) this.cells.set(key, { value: "", format: "General" }); return this.cells.get(key); }
    getRangeByIndexes(r, c, h, w) { return new Range(this, r, c, h, w); }
    getRange() { return this.getRangeByIndexes(0, 0, 1, 1); }
    delete() {
      // Office forbids deleting VeryHidden sheets, including recovery sheets.
      if (this.visibility === "VeryHidden") throw new Error("InvalidOperation: This operation is not permitted for the current object.");
      if (["cleanup-fail", "rollback-cleanup-fail"].includes(mode)) throw new Error("injected cleanup failure");
      sheets.delete(this.name);
    }
    makeTable(name, range) { return { name, style: "TableStyleMedium2", showTotals: false, showHeaders: true, load() {}, getRange: () => range,
      convertToRange: () => { this.tableItems = this.tableItems.filter(t => t.name !== name); return range; } }; }
  }
  const extract = new Sheet("Extract"), metadata = new Sheet("_precis_meta");
  sheets.set(extract.name, extract); sheets.set(metadata.name, metadata);
  const old = extract.getRangeByIndexes(report ? 2 : 0, 0,
    report ? (grouped ? 7 : 6) : 3, report ? 3 : 2);
  const reportValues = [["Report caption", "", ""], ["", "", ""], ["Department", "Revenue", ""], ["", "Actuals", "Var"],
    ["Group", "", ""], ["D1", 10, 2.5], ["", "", ""], ["Total", 10, 2.5]];
  if (!grouped) reportValues.splice(2, 2, ["Department", "Actuals", "Var"]);
  let originalValues = report ? reportValues.slice(2) : [["department", "Actuals"], ["D1", 10]];
  originalValues.push(["Old context", ...originalValues[0].slice(1).map(() => "")]);
  old.values = originalValues;
  if (report) extract.cell(0,0).value = "User maintained title";
  if (report && grouped) { extract.getRangeByIndexes(2, 0, 2, 1).merge(); extract.getRangeByIndexes(2, 1, 1, 2).merge(); }
  extract.getRangeByIndexes(0, 5, 1, 1).values = [["unrelated"]];
  extract.cell(2, 1).format = "0.00000";
  if (!report) extract.tableItems.push(extract.makeTable("tbl_extract", extract.getRangeByIndexes(0,0,old.rowCount-1,old.columnCount)));
  if (mode === "yaml") meta.sheets.Extract.source = region.source;
  metadata.getRange().values = [[mode === "yaml" ? require("yaml").stringify(meta) : JSON.stringify(meta)]];
  names.set("region", { name: "region", formula: "=" + old.address, load() {}, getRangeOrNullObject: () => old });
  names.set("ext_actuals", { name: "ext_actuals", formula: "=Extract!$B$2:$B$2", load() {}, getRangeOrNullObject: () => extract.getRangeByIndexes(1, 1, 1, 1) });
  const context = { workbook: {
    getSelectedRange: () => extract.getRangeByIndexes(report ? 4 : 1, 0, 1, 1),
    worksheets: { getItemOrNullObject: name => sheets.get(name) || { isNullObject: true, load() {} }, add: name => { const s = new Sheet(name); sheets.set(name, s); return s; } },
    names: { getItemOrNullObject: name => names.get(name) || { isNullObject: true, load() {} }, getItem: name => names.get(name), add: (name, range) => { const n = { name, formula: "=" + range.address, load() {}, delete: () => names.delete(name) }; names.set(name, n); return n; } },
  }, sync: async () => {
    const task = pending; pending = undefined; if (task) await task();
    if (mode === "commit-fail" && !commitFailed && JSON.parse(metadata.cell(0,0).value).sheets.Extract.regions[0].state === "live") {
      commitFailed = true; throw new Error("injected commit failure");
    }
  } };
  global.Excel = { run: fn => fn(context), SpecialCellType: { formulas: "formulas" }, SheetVisibility: { veryHidden: "VeryHidden", hidden: "Hidden", visible: "Visible" }, RangeCopyType: { all: "all", values: "values", formats: "formats" }, ClearApplyTo: { all: "All", contents: "Contents", formats: "Formats" } };
  global.fetch = async () => { fetches++; if (mode === "query-fail") throw new Error("offline");
    return { ok: true, status: 200, json: async () => ({ result: { structuredContent: {}, _meta: { "precis/worksheet": projection, "precis/renderBlock": block } } }) }; };
  config.setToken("session");
  return { extract, metadata, sheets, names, block, projection: reportProjection, worksheet: projection, original: JSON.stringify(old.values), old, fetches: () => fetches, calculationCalls: () => calculationCalls };
}

for (const layout of ["report", "extract"]) test(`${layout} footer is live and unwrapped, with title and value ranges preserved`, async () => {
  const f = fixture("ok",layout,"run_metric",true);
  await activateSelectedExtract(() => {});
  const spill = f.extract.spill;
  const footer = f.extract.cell(spill.rowIndex + spill.rowCount - 1,spill.columnIndex);
  assert.match(footer.value,/2026-01 to 2026-03/);
  assert.equal(footer.visual.wrapText,false);
  assert.equal(f.extract.rowHeights.has(spill.rowIndex + spill.rowCount - 1),false);
  assert.equal(JSON.parse(f.metadata.cell(0,0).value).sheets.Extract.regions[0].footer_rows,1);
  if (layout === "report") assert.equal(f.extract.cell(0,0).value,"User maintained title");
  else assert.match(f.names.get("ext_actuals").formula,/ROWS\('Extract'!\$A\$1#\)-1/);
});

test("conversion refuses a server without the footer contract before changing the workbook", async () => {
  const f=fixture();
  delete f.worksheet.footer_rows;
  await assert.rejects(activateSelectedExtract(() => {}),/supported financial footer/);
  assert.equal(JSON.stringify(f.old.values),f.original);
  assert.equal(f.calculationCalls(),0);
  assert.equal(f.extract.tableItems.length,1);
});

for (const layout of ["report", "extract"]) test(`${layout} exports without the footer contract require regeneration`, async () => {
  const f = fixture("ok", layout);
  const meta = JSON.parse(f.metadata.cell(0,0).value);
  delete meta.sheets.Extract.regions[0].shape.footer_rows;
  f.metadata.cell(0,0).value = JSON.stringify(meta);
  await assert.rejects(activateSelectedExtract(() => {}), /Regenerate the export/);
  assert.equal(f.fetches(), 0);
  assert.equal(JSON.stringify(f.old.values), f.original);
});

test("footer-bearing rollback restores Table data bounds and row heights",async () => {
  const f=fixture("commit-fail","extract","run_metric",true);
  f.extract.rowHeights.set(2,120);
  await assert.rejects(activateSelectedExtract(() => {}),/Original extract restored/);
  const restored=f.extract.tableItems[0].getRange();
  assert.equal(restored.rowCount,2); // header + data; the footer stays outside
  assert.equal(f.extract.rowHeights.get(2),120);
  assert.equal(f.extract.rowHeights.has(3),false);
  assert.equal(JSON.stringify(f.old.values),f.original);
  assert.equal(JSON.parse(f.metadata.cell(0,0).value).sheets.Extract.regions[0].state,"static");
});

test("footer growth resets the old height and preserves an adjacent note's taller row",async () => {
  const f=fixture("ok","extract","run_metric",true);
  f.extract.rowHeights.set(2,120);
  f.extract.rowHeights.set(3,180);
  f.extract.cell(3,10).value="Unrelated note";
  await activateSelectedExtract(() => {});
  assert.equal(f.extract.rowHeights.has(2),false);
  assert.equal(f.extract.rowHeights.get(3),180);
  assert.equal(f.extract.cell(3,10).value,"Unrelated note");
});

test("selected extract activates with one governed fetch, names and unrelated cells preserved", async () => {
  const f = fixture();
  const message = await activateSelectedExtract(() => {});
  assert.match(message, /Converted/);
  assert.equal(f.fetches(), 1);
  assert.equal(f.extract.cell(1, 1).value, 123.456789);
  assert.equal(f.extract.cell(0, 5).value, "unrelated");
  assert.equal(f.extract.tableItems.length, 0);
  assert.equal(JSON.parse(f.metadata.cell(0, 0).value).sheets.Extract.regions[0].state, "live");
  assert.match(f.names.get("ext_actuals").formula, /INDEX\('Extract'!\$A\$1#,2,2\)/);
  assert.equal(f.sheets.size, 2);
});

test("generated shared YAML metadata activates through the full selected-region path", async () => {
  const f = fixture("yaml", "report", "run_statement");
  assert.match(f.metadata.cell(0, 0).value, /&a1/);
  await activateSelectedExtract(() => {});
  assert.equal(f.fetches(), 1);
  assert.equal(JSON.parse(f.metadata.cell(0, 0).value).sheets.Extract.regions[0].state, "live");
});

test("preflight edits preserve the export and remove the VeryHidden recovery sheet safely", async () => {
  const f = fixture("preflight-edit");
  await assert.rejects(activateSelectedExtract(() => {}), /target changed during preflight/);
  assert.equal(f.extract.cell(1, 0).value, "user edit");
  assert.equal(f.extract.tableItems.length, 1);
  assert.equal(f.calculationCalls(), 0);
  assert.equal(f.sheets.size, 2);
});

for (const layout of ["extract", "report"]) test(`${layout} cleanup failure retains the committed live spill and exposes the saved original`, async () => {
  const f = fixture("cleanup-fail", layout), message = await activateSelectedExtract(() => {});
  assert.match(message, /Converted/);
  assert.match(message, /Recovery-sheet cleanup could not be confirmed.*injected cleanup failure/);
  assert.equal(JSON.parse(f.metadata.cell(0, 0).value).sheets.Extract.regions[0].state, "live");
  assert.match(f.extract.cell(layout === "report" ? 2 : 0, 0).formula, /^=PRECIS\./);
  assert.ok(f.extract.spill);
  const backup = [...f.sheets.values()].find(s => s.name.startsWith("_PrecisRecovery_"));
  assert.equal(backup.visibility, "Visible");
  const first = layout === "report" ? 2 : 0;
  assert.deepEqual(backup.getRangeByIndexes(first, 0, f.old.rowCount, f.old.columnCount).values, JSON.parse(f.original));
});

test("rollback cleanup failure reports restored cells and retains a visible recovery copy", async () => {
  const f = fixture("rollback-cleanup-fail"), originalMeta = f.metadata.cell(0, 0).value;
  await assert.rejects(activateSelectedExtract(() => {}), error => {
    assert.match(error.message, /Original extract restored/);
    assert.match(error.message, /Recovery-sheet cleanup could not be confirmed/);
    assert.doesNotMatch(error.message, /recovery is incomplete/);
    return true;
  });
  assert.equal(JSON.stringify(f.old.values), f.original);
  assert.equal(f.metadata.cell(0, 0).value, originalMeta);
  assert.equal(f.extract.tableItems[0].name, "tbl_extract");
  assert.equal([...f.sheets.values()].find(s => s.name.startsWith("_PrecisRecovery_")).visibility, "Visible");
});

test("incomplete restoration reports both conversion and recovery errors", async () => {
  const f = fixture("restore-fail", "report");
  await assert.rejects(activateSelectedExtract(() => {}), error => {
    assert.match(error.message, /recovery is incomplete/);
    assert.match(error.message, /Conversion error: Error: injected calculation failure/);
    assert.match(error.message, /Recovery error: Error: injected restore failure/);
    return true;
  });
  const backup = [...f.sheets.values()].find(s => s.name.startsWith("_PrecisRecovery_"));
  assert.equal(backup.visibility, "Visible");
  assert.equal(backup.cell(4, 0).value, "Group");
  assert.equal(backup.merges.length, 2);
  assert.equal(f.extract.cell(0, 0).value, "User maintained title");
  assert.equal(f.extract.merges.length, 0);
});

test("recorded legacy Tables without region metadata require regeneration", async () => {
  const f = fixture();
  const meta = JSON.parse(f.metadata.cell(0, 0).value);
  meta.sheets.Extract.source = meta.sheets.Extract.regions[0].source;
  meta.sheets.Extract.excel_tables = [{ name: "tbl_extract" }];
  delete meta.sheets.Extract.regions;
  f.metadata.cell(0, 0).value = JSON.stringify(meta);
  await assert.rejects(activateSelectedExtract(() => {}), /region boundary.*Regenerate/);
  assert.equal(f.fetches(), 0);
  assert.equal(f.extract.tableItems.length, 1);
  assert.equal(JSON.stringify(f.old.values), f.original);
});

test("failed query leaves Table, values and metadata untouched", async () => {
  const f = fixture("query-fail"), originalMeta = f.metadata.cell(0, 0).value;
  await assert.rejects(activateSelectedExtract(() => {}), /offline/);
  assert.equal(JSON.stringify(f.old.values), f.original);
  assert.equal(f.extract.tableItems.length, 1);
  assert.equal(f.metadata.cell(0, 0).value, originalMeta);
  assert.equal(f.calculationCalls(), 0);
});

test("calculation failure restores original extract and growth-area number formats", async () => {
  const f = fixture("fail"), originalMeta = f.metadata.cell(0, 0).value;
  await assert.rejects(activateSelectedExtract(() => {}), /Original extract restored/);
  assert.equal(JSON.stringify(f.old.values), f.original);
  assert.equal(f.extract.tableItems[0].name, "tbl_extract");
  assert.equal(f.metadata.cell(0, 0).value, originalMeta);
  assert.equal(f.extract.cell(2, 1).format, "0.00000");
  assert.equal(f.sheets.size, 2);
});

test("competing growth-cell edits survive blocked-spill recovery", async () => {
  const f = fixture("growth-edit"), realNow = Date.now;
  let now = 0; Date.now = () => now += 16000;
  try { await assert.rejects(activateSelectedExtract(() => {}), /Changes were preserved/); }
  finally { Date.now = realNow; }
  assert.equal(f.extract.cell(2, 1).value, "user edit");
  assert.equal(JSON.parse(f.metadata.cell(0, 0).value).sheets.Extract.regions[0].state, "recovery");
  assert.equal(f.sheets.size, 3);
  assert.equal([...f.sheets.values()].find(s => s.name.startsWith("_PrecisRecovery_")).visibility, "Visible");
});

test("mismatched instance and rebound names are refused before conversion", async () => {
  let f = fixture();
  let meta = JSON.parse(f.metadata.cell(0, 0).value); meta.sheets.Extract.regions[0].origin = "https://other.example";
  f.metadata.cell(0, 0).value = JSON.stringify(meta);
  await assert.rejects(activateSelectedExtract(() => {}), /different Précis instance/);
  assert.equal(f.fetches(), 0);
  f = fixture(); f.names.get("ext_actuals").getRangeOrNullObject = () => f.extract.getRangeByIndexes(3, 5, 1, 1);
  await assert.rejects(activateSelectedExtract(() => {}), /rebound/);
  assert.equal(f.extract.tableItems.length, 1);
  assert.equal(f.calculationCalls(), 0);
});

test("late unrelated metadata changes are never overwritten by commit or recovery", async () => {
  const f = fixture("metadata-edit");
  await assert.rejects(activateSelectedExtract(() => {}), /Changes were preserved/);
  assert.equal(JSON.parse(f.metadata.cell(0, 0).value).sheets.Unrelated.note, "concurrent provenance edit");
  assert.equal(f.sheets.size, 3);
  assert.equal(f.extract.cell(1, 1).value, 123.456789);
});

for (const tool of ["run_metric", "run_statement"]) test(`${tool} report activation keeps report layout, precision and selected scope`, async () => {
  const f = fixture("ok", "report", tool);
  f.extract.cell(0, 0).format = "caption format";
  f.extract.cell(2, 1).format = "header format";
  const message = await activateSelectedExtract(() => {});
  assert.match(message, /live report formula/);
  assert.equal(f.fetches(), 1);
  const spill = f.extract.spill;
  assert.equal(spill.rowIndex, 2);
  assert.deepEqual(spill.values, f.projection.grid);
  assert.deepEqual(spill.numberFormat, f.projection.number_formats);
  assert.match(spill.formulas[0][0], /"report"/);
  assert.doesNotMatch(spill.formulas[0][0], /,FALSE\)$/);
  assert.equal(f.extract.merges.length, 0);
  assert.equal(f.extract.cell(0,0).value,"User maintained title");
  assert.equal(f.extract.cell(0, 0).format, "caption format");
  assert.equal(f.extract.cell(4, 0).value, "Group"); // First data row is unchanged.
  assert.equal(f.extract.cell(7, 1).value, ""); // Old footer cleared, no row deletion.
  assert.equal(f.extract.cell(0, 5).value, "unrelated");
  const region = JSON.parse(f.metadata.cell(0, 0).value).sheets.Extract.regions[0];
  assert.equal(region.layout, "report"); assert.equal(region.state, "live"); assert.equal(region.source, undefined);
  assert.equal(f.sheets.size, 2);
});

test("failed report calculation restores the title, semantic blank rows, merges, formats and provenance", async () => {
  const f = fixture("fail", "report"), originalMeta = f.metadata.cell(0, 0).value;
  const addresses = f.extract.merges.map(m => m.address);
  f.extract.cell(5, 2).format = '0.00"%"';
  await assert.rejects(activateSelectedExtract(() => {}), /Original extract restored/);
  assert.equal(JSON.stringify(f.old.values), f.original);
  assert.deepEqual(f.extract.merges.map(m => m.address), addresses);
  assert.equal(f.extract.cell(5, 2).format, '0.00"%"');
  assert.equal(f.metadata.cell(0, 0).value, originalMeta);
  assert.equal(f.extract.tableItems.length, 0);
  assert.equal(f.sheets.size, 2);
});

test("single-header reports retain the title and anchor the live grid at the column-header row", async () => {
  const f = fixture("ok", "report", "run_statement", false);
  await activateSelectedExtract(() => {});
  assert.equal(f.extract.spill.rowIndex, 2);
  assert.equal(f.extract.cell(0,0).value,"User maintained title");
  assert.deepEqual(f.extract.spill.values, f.projection.grid);
  assert.equal(f.extract.cell(3, 0).value, "Group");
});

test("fresh report growth keeps the data anchor and refuses occupied growth cells", async () => {
  for (const obstructed of [false, true]) {
    const f = fixture("ok", "report");
    f.projection.grid.splice(-1,0,["New 1", 1, 2], ["New 2", 3, 4]);
    f.projection.number_formats.splice(-1,0,["General", "0.00", "0.00"], ["General", "0.00", "0.00"]);
    f.projection.data_rows += 2;
    f.block.rows.push({ row_type: "line_item", label: "New 1", values: { Actuals: 1, Var: 2 } },
      { row_type: "line_item", label: "New 2", values: { Actuals: 3, Var: 4 } });
    if (obstructed) {
      f.extract.cell(9, 1).value = "user note";
      await assert.rejects(activateSelectedExtract(() => {}), /overwrite neighbouring content/);
      assert.equal(f.extract.cell(9, 1).value, "user note");
      assert.equal(JSON.stringify(f.old.values), f.original);
    } else {
      await activateSelectedExtract(() => {});
      assert.equal(f.extract.spill.rowIndex, 2);
      assert.equal(f.extract.spill.rowCount, 8);
      assert.equal(f.extract.cell(8, 0).value, "New 2");
      assert.equal(f.extract.cell(0,0).value,"User maintained title");
    }
  }
});

test("report boundary and header drift are refused before workbook mutation", async () => {
  for (const change of ["width", "offset", "rows", "header"]) {
    const f = fixture("ok", "report");
    const meta = JSON.parse(f.metadata.cell(0, 0).value), shape = meta.sheets.Extract.regions[0].shape;
    if (change === "width") f.names.get("region").getRangeOrNullObject = () => f.extract.getRangeByIndexes(0, 0, 8, 4);
    if (change === "offset") shape.data_start_row = 100;
    if (change === "rows") shape.row_count++;
    if (change === "header") [f.extract.cell(3, 1).value, f.extract.cell(3, 2).value] = [f.extract.cell(3, 2).value, f.extract.cell(3, 1).value];
    f.metadata.cell(0, 0).value = JSON.stringify(meta);
    const original = JSON.stringify(f.old.values);
    await assert.rejects(activateSelectedExtract(() => {}), /reliable data-start boundary|headers changed/);
    assert.equal(f.calculationCalls(), 0);
    assert.equal(f.sheets.size, 2);
    assert.equal(JSON.stringify(f.old.values), original);
  }
});

test("concurrent title edits survive both conversion and restoration", async () => {
  for (const mode of ["caption-edit", "fail-caption-edit"]) {
    const f = fixture(mode, "report");
    if (mode === "caption-edit") await activateSelectedExtract(() => {});
    else {
      await assert.rejects(activateSelectedExtract(() => {}), /Original extract restored/);
      assert.deepEqual(f.old.values.slice(2), JSON.parse(f.original).slice(2));
      assert.equal(f.extract.merges.length, 2);
    }
    assert.equal(f.extract.cell(0, 0).value, "user caption edit");
    assert.equal(f.sheets.size, 2);
  }
});

test("session changes before commit restore the original body and preserve the new session", async () => {
  const f = fixture("session-edit", "report"), originalMeta = f.metadata.cell(0, 0).value;
  await assert.rejects(activateSelectedExtract(() => {}), /Original extract restored.*session changed/);
  assert.equal(config.getToken(), "replacement-session");
  assert.equal(JSON.stringify(f.old.values), f.original);
  assert.equal(f.metadata.cell(0, 0).value, originalMeta);
});

test("merges crossing the report boundary and legacy reports are refused before mutation", async () => {
  let f = fixture("ok", "report");
  f.extract.getRangeByIndexes(7, 1, 2, 2).merge();
  await assert.rejects(activateSelectedExtract(() => {}), /merge outside the report column headers/);
  assert.equal(f.calculationCalls(), 0);
  assert.equal(JSON.stringify(f.old.values), f.original);
  f = fixture("ok", "report");
  const meta = JSON.parse(f.metadata.cell(0, 0).value); delete meta.sheets.Extract.regions;
  f.metadata.cell(0, 0).value = JSON.stringify(meta);
  await assert.rejects(activateSelectedExtract(() => {}), /boundary/);
  assert.equal(f.fetches(), 0);
});

test("a competing body merge survives and recovery retains the original headers and body", async () => {
  const f = fixture("merge-edit", "report");
  await assert.rejects(activateSelectedExtract(() => {}), /Changes were preserved/);
  assert.deepEqual(f.extract.merges.map(m => m.address), ["Extract!B8:C8"]);
  const backup = [...f.sheets.values()].find(s => s.name.startsWith("_PrecisRecovery_"));
  assert.equal(backup.visibility, "Visible");
  assert.equal(backup.cell(4, 0).value, "Group");
  assert.equal(backup.merges.length, 2);
  assert.equal(f.extract.cell(0, 0).value, "User maintained title");
});

test("report ribbon formatting retains the server's per-cell units and decimals", async () => {
  const f = fixture("ok", "report");
  await activateSelectedExtract(() => {});
  // Give row and column legacy nf conflicting values: resolved cell_nf must win.
  f.block.rows[1].nf = "#,##0"; f.block.columns[2].nf = '0.0"%"';
  const { applyBlockFormat } = require("../src/format.ts");
  applyBlockFormat(f.extract.spill, f.block);
  assert.equal(f.extract.spill.numberFormat[3][2], '0.00"%"');
  assert.equal(f.extract.spill.numberFormat[0][0], "General");
  assert.equal(f.extract.cell(5, 2).value, 8.7654321);
});
