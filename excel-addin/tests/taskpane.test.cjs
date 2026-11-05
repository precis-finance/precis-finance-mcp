const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const ts = require("typescript");
require.extensions[".ts"] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019 }, fileName: filename,
}).outputText, filename);
const config = require("../src/config.ts");

function pane(supported = true, connected = true) {
  delete global.__precis;
  global.window = { location: { href: "https://finance.example/excel/taskpane.html" } };
  const nodes = new Map();
  const ids = new Set([...fs.readFileSync(require.resolve("../src/taskpane/taskpane.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
  const node = id => {
    assert.ok(ids.has(id), `Task pane has element ${id}`);
    if (!nodes.has(id)) nodes.set(id, { textContent: id === "convert-live" ? "Convert selected export to live formula" : "", style: {}, dataset: {},
      disabled: false, attributes: {}, scrolls: 0, setAttribute(key, value) { this.attributes[key] = value; },
      focus() { document.activeElement = this; },
      scrollIntoView() { this.scrolls++; }, appendChild() {} });
    return nodes.get(id);
  };
  global.document = { getElementById: node, createElement: () => ({}) };
  global.Office = { onReady: fn => fn(), context: { requirements: { isSetSupported: () => supported },
    document: { settings: { set() {}, saveAsync() {} } } } };
  global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ result: { structuredContent: { dimensions: [] } } }) });
  if (connected) config.setToken("test-session");
  const name = require.resolve("../src/taskpane/taskpane.ts");
  delete require.cache[name];
  require(name);
  return node;
}

test("conversion shows immediate inline progress, busy state and completion", async () => {
  const node = pane();
  let finish;
  global.Excel = { run: () => new Promise(resolve => finish = resolve) };
  const pending = node("convert-live").onclick();
  assert.equal(node("conversion-feedback").textContent, "Checking the selected export…");
  assert.equal(node("conversion-feedback").attributes.role, "status");
  assert.equal(node("convert-live").disabled, true);
  assert.equal(node("convert-live").textContent, "Converting…");
  assert.equal(node("convert-live").attributes["aria-busy"], "true");
  finish("Converted selected export to a live formula.");
  await pending;
  assert.match(node("conversion-feedback").textContent, /^Converted/);
  assert.equal(node("conversion-feedback").dataset.state, "success");
  assert.ok(node("conversion-feedback").scrolls > 0);
  assert.equal(node("convert-live").disabled, false);
  assert.equal(node("convert-live").attributes["aria-busy"], "false");
});

test("Excel selection failures appear beside the button and controls recover", async () => {
  const node = pane();
  global.Excel = { run: async () => { throw new Error("Select cells wholly inside one generated Précis export."); } };
  await node("convert-live").onclick();
  assert.equal(node("conversion-feedback").attributes.role, "alert");
  assert.match(node("conversion-feedback").textContent, /Conversion stopped: Select cells/);
  assert.equal(node("conversion-feedback").dataset.state, "error");
  for (const id of ["convert-live", "refresh", "insert-dropdown", "disconnect"]) assert.equal(node(id).disabled, false);
});

test("unsupported Excel versions explain the disabled conversion action", () => {
  const node = pane(false);
  assert.equal(node("convert-live").disabled, true);
  assert.match(node("conversion-support").textContent, /Conversion is unavailable.*ExcelApi 1.13/);
});

test("tabs retain member choices and provide keyboard navigation with one tab stop", () => {
  const node = pane();
  assert.equal(node("tab-connection").attributes["aria-selected"], "true");
  assert.equal(node("panel-members").hidden, true);
  node("tab-members").onclick();
  node("dropdown-dim").value = "department";
  assert.equal(node("panel-members").hidden, false);
  assert.equal(node("panel-connection").hidden, true);
  let prevented = false;
  node("tab-members").onkeydown({ key: "ArrowRight", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(document.activeElement, node("tab-convert"));
  assert.equal(node("tab-convert").tabIndex, 0);
  assert.equal(node("tab-members").tabIndex, -1);
  assert.equal(node("panel-convert").hidden, false);
  node("tab-convert").onkeydown({ key: "ArrowRight", preventDefault() {} });
  assert.equal(document.activeElement, node("tab-connection"));
  node("tab-connection").onkeydown({ key: "End", preventDefault() {} });
  assert.equal(document.activeElement, node("tab-convert"));
  node("tab-convert").onkeydown({ key: "Home", preventDefault() {} });
  assert.equal(document.activeElement, node("tab-connection"));
  node("tab-members").onclick();
  assert.equal(node("dropdown-dim").value, "department");
  assert.equal(node("connection-state").textContent, "Connected");
});

test("disconnected tabs explain sign-in and navigate to Connection", () => {
  const node = pane(true, false);
  assert.equal(node("connection-state").textContent, "Not connected");
  for (const tab of ["members", "convert"]) {
    node(`tab-${tab}`).onclick();
    assert.equal(node(`${tab}-signin`).hidden, false);
    assert.equal(node(`${tab}-controls`).hidden, true);
    node(`${tab}-connect`).onclick();
    assert.equal(node("panel-connection").hidden, false);
    assert.equal(document.activeElement, node("signin"));
  }
  assert.equal(node("convert-live").disabled, true);
  assert.equal(node("insert-dropdown").disabled, true);
});

test("member feedback stays in the Members tab and tab changes preserve conversion errors", async () => {
  const node = pane();
  node("tab-members").onclick();
  await node("insert-dropdown").onclick();
  assert.equal(node("members-feedback").textContent, "Pick a dimension first.");
  global.Excel = { run: async () => { throw new Error("blocked spill"); } };
  node("tab-convert").onclick();
  await node("convert-live").onclick();
  node("tab-connection").onclick();
  node("tab-convert").onclick();
  assert.match(node("conversion-feedback").textContent, /blocked spill/);
});

test("session loss during conversion keeps signed-out controls disabled on completion", async () => {
  const node = pane();
  let finish;
  global.Excel = { run: () => new Promise(resolve => finish = resolve) };
  const pending = node("convert-live").onclick();
  config.disconnect();
  assert.equal(node("connection-state").textContent, "Not connected");
  assert.equal(node("convert-signin").hidden, false);
  finish("Conversion ended.");
  await pending;
  for (const id of ["convert-live", "refresh", "insert-dropdown", "disconnect"]) assert.equal(node(id).disabled, true);
});
