const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const ts = require("typescript");
// Exercise the actual pure TS modules without introducing another build/test
// framework. Production bundling and static type checks run separately.
require.extensions[".ts"] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 }, fileName: filename,
}).outputText, filename);
const { extractDefinition, parseMeta, retireStaticRegion, inside, overlaps, sameGrid } = require("../src/regions/contract.ts");
const { applyCommonArgs, parseScenario, statement, metric } = (() => {
  global.CustomFunctions = { associate() {} };
  return require("../src/functions/functions.ts");
})();
const { prepareRead, takePreparedRead, readSignature, setToken, disconnect } = require("../src/config.ts");
const source = (tool = "run_metric") => ({ tool, params: {
  metrics: ["revenue", "margin"], statement: "pnl", period_start: "2026-01", period_end: "2026-03",
  scenarios: [{ scenario: "BUD-2026&uncommitted", alias: 'Budget "draft"' }], dimensions: ["department"],
  filters: { department: ["D1", "D2"] }, scale: 0, decimals: 0,
} });

test("shared Python and TypeScript formula contract fixtures", () => {
  for (const fixture of require("./fixtures/financial-formulas.json")) {
    assert.equal(extractDefinition(fixture.source, undefined, fixture.layout).formula, fixture.formula);
  }
});

test("formula argument orders preserve zeros, quotes, aliases and modifiers", () => {
  const m = extractDefinition(source());
  assert.equal(m.formula, '=PRECIS.METRIC("revenue,margin","2026-01","2026-03","department","BUD-2026&uncommitted as Budget ""draft""","department=D1|D2","extract",0,0)');
  const s = extractDefinition(source("run_statement"));
  assert.equal(s.formula, '=PRECIS.STATEMENT("pnl","2026-01","2026-03","BUD-2026&uncommitted as Budget ""draft""","department","department=D1|D2",0,0,"extract")');
  const args = {};
  applyCommonArgs(args, 'BUD-2026&uncommitted as Budget "draft"', "department", "department=D1|D2", 0, 0);
  for (const key of ["scenarios", "dimensions", "filters", "scale", "decimals"]) assert.deepEqual(args[key], m.args[key]);
});

test("missing global decimals use the normal default without an extra formula argument", () => {
  const src = source(); src.params.decimals = null;
  const d = extractDefinition(src);
  assert.equal(d.args.decimals, 1);
  assert.ok(d.formula.endsWith('0,1)'));
});

test("report definitions preserve both formula argument orders and region layout", () => {
  for (const tool of ["run_metric", "run_statement"]) {
    const definition = extractDefinition(source(tool), undefined, "report");
    assert.equal(definition.tool, tool);
    assert.match(definition.formula, /"report"/);
    assert.doesNotMatch(definition.formula, /"extract"/);
  }
  const meta = { sheets: { S: { regions: [{ range_name: "report", layout: "report", state: "static", source: source() }] } } };
  assert.equal(retireStaticRegion(meta, "S", "report", { row: 0, column: 0, rows: 2, columns: 2 }).sheets.S.regions[0].layout, "report");
});

test("normal report formulas include column headers after reload and recalculation", async () => {
  global.window = { location: { href: "https://finance.example/excel/taskpane.html" } };
  setToken("session");
  const report = { version: 1, grid: [["", "Actuals"], ["Line", "Actuals"], ["Revenue", 123.456789], ["Context", ""]],
    header_rows: 2, data_rows: 1, footer_rows: 1, number_formats: [["General", "General"], ["General", "General"], ["General", "#,##0.00"], ["General", "General"]] };
  global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ result: { _meta: {
    "precis/worksheet": { version: 1, header_rows: 1, data_rows: 1, footer_rows: 1, grid: [["key", "Actuals"], ["revenue", 123.456789], ["Context", ""]], number_formats: [["General", "General"], ["General", "#,##0.00"], ["General", "General"]], report },
    "precis/renderBlock": { type: "financial_table", columns: [{ key: "label" }, { key: "Actuals" }], rows: [{ label: "Revenue", values: { Actuals: 123.456789 } }] },
  } } }) });
  for (const fn of [statement, metric]) {
    const args = fn === statement ? ["pnl", "2026-01", "2026-03", "actuals", "", "", 0, 2, "report"] :
      ["revenue", "2026-01", "2026-03", "", "actuals", "", "report", 0, 2];
    assert.deepEqual(await fn(...args, { address: "S!A3" }), report.grid);
    disconnect(); setToken("session"); // No prepared-read or block cache survives.
    assert.deepEqual(await fn(...args, { address: "S!A3" }), report.grid);
  }
});

test("ambiguous encodings and unresolved defaults are rejected rather than changed", () => {
  for (const mutate of [
    p => delete p.scale,
    p => delete p.decimals,
    p => p.filters.department = 123,
    p => p.filters.department = ["D1"],
    p => p.metrics = ["revenue,margin"],
    p => p.scenarios[0].scenario = "budget as Other",
    p => p.scenarios[0].additional = true,
  ]) { const s = source(); mutate(s.params); assert.throws(() => extractDefinition(s)); }
  assert.throws(() => extractDefinition(source(), { scale: 3, decimals: 0 }), /Conflicting/);
});

test("only selected source metadata is retired and legacy refresh cannot overwrite it", () => {
  const meta = { precis_meta_version: 1, sheets: { S: { kind: "extract", source: source(), shape: {},
    regions: [{ range_name: "selected", source: source(), state: "static" }, { range_name: "other", source: source(), state: "static" }] }, Other: { kind: "user" } } };
  const changed = retireStaticRegion(meta, "S", "selected", { row: 0, column: 0, rows: 3, columns: 2 });
  assert.equal(changed.sheets.S.regions[0].state, "live");
  assert.equal(changed.sheets.S.regions[0].source, undefined);
  assert.equal(changed.sheets.S.source, undefined);
  assert.deepEqual(changed.sheets.S.regions[1], meta.sheets.S.regions[1]);
  assert.deepEqual(changed.sheets.Other, meta.sheets.Other);
  assert.equal(meta.sheets.S.regions[0].state, "static");
});

test("live markers retain instance and generated-name identities without a replay source", () => {
  const meta = { sheets: { S: { regions: [{ range_name: "region", source: source(), origin: "https://finance.example", named_ranges: [{name:"ext_actuals",ref:"S!B2:B3"}] }] } } };
  const live = retireStaticRegion(meta,"S","region",{row:0,column:0,rows:3,columns:2}).sheets.S.regions[0];
  assert.equal(live.origin,"https://finance.example");
  assert.deepEqual(live.named_ranges,meta.sheets.S.regions[0].named_ranges);
  assert.equal(live.source,undefined);
});

test("metadata parsing rejects cycles, duplicate keys, versions and oversized cells", () => {
  assert.equal(parseMeta('precis_meta_version: 1\nsheets: { S: {kind: extract} }').sheets.S.kind, "extract");
  for (const raw of ['precis_meta_version: 2\nsheets: {}', 'precis_meta_version: 1\nsheets: {}\nsheets: {}',
    'precis_meta_version: 1\nsheets: &s { S: *s }', "x".repeat(32768)]) assert.throws(() => parseMeta(raw));
});

test("metadata accepts the ordinary shared source emitted by PyYAML", () => {
  const meta = parseMeta(`precis_meta_version: 1
sheets:
  Report:
    kind: report
    source: &id001
      tool: run_statement
      params: {statement: pnl, scale: 3, decimals: 1}
    regions:
      - range_name: report_region
        state: static
        layout: report
        source: *id001
`);
  assert.deepEqual(meta.sheets.Report.regions[0].source, meta.sheets.Report.source);
  const changed = retireStaticRegion(meta, "Report", "report_region", { row: 0, column: 0, rows: 3, columns: 2 });
  assert.equal(changed.sheets.Report.source, undefined);
  assert.equal(changed.sheets.Report.regions[0].state, "live");
});

test("metadata alias and expansion limits remain bounded", () => {
  const base = "precis_meta_version: 1\nsheets: {}\n";
  for (const text of [
    base + "note: *missing\n",
    base + "note: &a [*a]\n",
    base + "a: &a [one]\nb: [" + Array(101).fill("*a").join(",") + "]\n",
    base + 'a: &a "' + "x".repeat(1000) + '"\nb: [' + Array(40).fill("*a").join(",") + "]\n",
    base + "note: " + "[".repeat(55) + "1" + "]".repeat(55),
    base + "note: .inf\n",
  ]) assert.throws(() => parseMeta(text), /broken, circular or oversized/);
});

test("selection containment does not choose spanning or overlapping regions", () => {
  const a = { row: 2, column: 2, rows: 3, columns: 2 };
  assert.equal(inside({ row: 3, column: 3, rows: 1, columns: 1 }, a), true);
  assert.equal(inside({ row: 3, column: 3, rows: 3, columns: 2 }, a), false);
  assert.equal(overlaps(a, { row: 5, column: 2, rows: 2, columns: 2 }), false);
});

test("spill comparison tolerates Excel numeric precision without masking changed data", () => {
  assert.equal(sameGrid([[1.23456789012346, "key", null]], [[1.2345678901234567, "key", ""]]), true);
  assert.equal(sameGrid([[123.45]], [[123.46]]), false);
  assert.equal(sameGrid([[0]], [[""]]), false);
  assert.equal(sameGrid([["Label"]], [["label"]]), false);
});

test("prepared reads are anchor-normalised, argument-bound, single-use and cleared on sign-out", () => {
  const signature = readSignature("run_metric_data", { scale: 0, metrics: ["revenue"] });
  prepareRead("'Sheet 1'!$A$1", signature, { prepared: true });
  assert.deepEqual(takePreparedRead("Sheet 1!A1", signature), { prepared: true });
  assert.equal(takePreparedRead("Sheet 1!A1", signature), undefined);
  prepareRead("S!A1", signature, { prepared: true });
  assert.equal(takePreparedRead("S!A1", "different"), undefined);
  prepareRead("S!A1", signature, {});
  disconnect();
  assert.equal(takePreparedRead("S!A1", signature), undefined);
});

test("cold formatting never substitutes report layout or default decimals", async () => {
  global.Office = { onReady() {}, actions: { associate() {} } };
  const { refetchFromFormula } = require("../src/commands/commands.ts");
  for (const formula of [
    '=PRECIS.STATEMENT("pnl","2026-01","2026-03","actuals","","",0,0,"extract")',
    '=PRECIS.METRIC("revenue","2026-01","2026-03","","actuals","","extract",0,0)',
  ]) await assert.rejects(refetchFromFormula(formula), /Refresh data/);
});

test("actual financial functions consume prepared arguments and preserve statement extract layout", async () => {
  global.window = { location: { href: "https://finance.example/excel/taskpane.html" } };
  setToken("session");
  const projection = { version: 1, header_rows: 1, data_rows: 1, footer_rows: 1, grid: [["department", "metric", "Actuals"], ["D1", "revenue", 123.456789], ["Context", "", ""]],
    number_formats: [["General", "General", "General"], ["General", "General", "0.00"], ["General", "General", "General"]] };
  const envelope = { _meta: { "precis/worksheet": projection } };
  const definition = extractDefinition(source());
  prepareRead("'S'!$A$1", readSignature(definition.tool, definition.args), envelope);
  const calls = [];
  global.fetch = async (_url, options) => { calls.push(JSON.parse(options.body)); return { ok: true, status: 200, json: async () => ({ result: envelope }) }; };
  const grid = await metric("revenue,margin", "2026-01", "2026-03", "department", 'BUD-2026&uncommitted as Budget "draft"', "department=D1|D2", "extract", 0, 0, { address: "S!A1" });
  assert.deepEqual(grid, projection.grid);
  assert.equal(calls.length, 0);
  await statement("pnl", "2026-01", "2026-03", "actuals", "department", "", 0, NaN, "extract", { address: "S!B1" });
  assert.equal(calls[0].params.name, "run_statement_data");
  assert.equal(calls[0].params.arguments.decimals, undefined);
  assert.equal(calls[0].params.arguments.scale, 0);
  assert.deepEqual(calls[0].params._meta["precis/worksheet"], {version: 1});
});
