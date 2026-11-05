import { parseDocument } from "yaml";

export interface Bounds { row: number; column: number; rows: number; columns: number }
export interface ExtractDefinition {
  tool: "run_metric_data" | "run_statement_data" | "run_metric" | "run_statement";
  args: Record<string, unknown>;
  formula: string;
}

export function inside(inner: Bounds, outer: Bounds): boolean {
  return inner.row >= outer.row && inner.column >= outer.column &&
    inner.row + inner.rows <= outer.row + outer.rows && inner.column + inner.columns <= outer.column + outer.columns;
}

export function overlaps(a: Bounds, b: Bounds): boolean {
  return a.row < b.row + b.rows && b.row < a.row + a.rows &&
    a.column < b.column + b.columns && b.column < a.column + a.columns;
}

/** Excel normalises numbers to its cell precision; strings/zeros remain exact. */
export function sameGrid(actual: unknown[][], expected: unknown[][]): boolean {
  return actual.length === expected.length && actual.every((row, r) => row.length === expected[r].length && row.every((cell, c) => {
    const wanted = expected[r][c];
    if (typeof cell === "number" && typeof wanted === "number") {
      return Number.isFinite(cell) && Number.isFinite(wanted) && Math.abs(cell - wanted) <= Math.max(Math.abs(cell), Math.abs(wanted), Number.MIN_VALUE) * 1e-14;
    }
    return cell === wanted || (cell == null || cell === "") && (wanted == null || wanted === "");
  }));
}

export function parseMeta(raw: string): any {
  if (!raw || raw.length > 32767) throw new Error("Missing or oversized Précis metadata. Regenerate the extract.");
  const doc = parseDocument(raw, { uniqueKeys: true, stringKeys: true });
  if (doc.errors.length) throw new Error("Invalid Précis metadata. Regenerate the extract.");
  let meta: any;
  try {
    // PyYAML exports share sheet/region source objects through ordinary aliases.
    // Permit those, while bounding expansion before JSON serialization or use.
    meta = doc.toJS({ maxAliasCount: 100 });
    const ancestors = new Set<object>();
    let nodes = 0, characters = 0;
    const check = (value: any, depth = 0): void => {
      if (++nodes > 10000 || depth > 50) throw new Error("Metadata nesting limit");
      if (value && typeof value === "object") {
        if (ancestors.has(value)) throw new Error("Circular metadata reference");
        ancestors.add(value);
        for (const [key, child] of Object.entries(value)) {
          characters += key.length;
          check(child, depth + 1);
        }
        ancestors.delete(value);
      } else {
        if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Invalid metadata number");
        characters += String(value).length;
      }
      if (characters > 32767) throw new Error("Expanded metadata limit");
    };
    check(meta);
    if (JSON.stringify(meta).length > 32767) throw new Error("Expanded metadata limit");
  } catch {
    throw new Error("Précis metadata contains broken, circular or oversized shared references. Regenerate the export.");
  }
  if (meta?.precis_meta_version !== 1 || !meta.sheets || typeof meta.sheets !== "object" || Array.isArray(meta.sheets)) {
    throw new Error("Unsupported Précis metadata. Regenerate the extract.");
  }
  return meta;
}

function atom(value: unknown, forbidden: RegExp, label: string): string {
  if (typeof value !== "string" || !value || value.trim() !== value || forbidden.test(value)) {
    throw new Error(`${label} cannot be represented by this version of the add-in. Regenerate with supported arguments.`);
  }
  return value;
}

const quote = (value: string): string => `"${value.replace(/"/g, '""')}"`;

/** Strict, lossless subset of the existing worksheet argument grammar. */
export function extractDefinition(source: any, presentation?: any, layout: "report" | "extract" = "extract"): ExtractDefinition {
  if (source?.tool !== "run_statement" && source?.tool !== "run_metric") throw new Error("Select a reporting-catalogue statement or metric extract.");
  const p = source.params;
  if (!p || typeof p !== "object" || Array.isArray(p)) throw new Error("Missing extract source arguments.");
  const supported = new Set(["statement", "metrics", "period_start", "period_end", "scenarios", "dimensions", "filters", "scale", "decimals"]);
  const workflow = new Set(["out", "target", "layout", "filename", "sheet_name", "overwrite", "report_id", "position"]);
  for (const key of Object.keys(p)) if (!supported.has(key) && !workflow.has(key)) throw new Error(`Unsupported source argument: ${key}.`);
  for (const key of ["period_start", "period_end"]) {
    if (typeof p[key] !== "string" || !/^\d{4}-(0[1-9]|1[0-2])$/.test(p[key])) throw new Error("The extract needs explicit resolved monthly periods. Regenerate it.");
  }
  const list = (value: unknown, label: string): string[] => {
    if (!Array.isArray(value)) throw new Error(`Missing or unsupported ${label}. Regenerate the extract.`);
    return value.map(v => atom(v, /[,\r\n\t]/, label));
  };
  if (!Array.isArray(p.scenarios) || !p.scenarios.length) throw new Error("The extract needs explicit resolved scenarios. Regenerate it.");
  const scenarioText = p.scenarios.map((s: any) => {
    if (!s || typeof s !== "object" || Array.isArray(s) || Object.keys(s).some(k => k !== "scenario" && k !== "alias")) throw new Error("Unsupported scenario definition.");
    const key = atom(s.scenario, /,|\s+as\s+|[\r\n\t]/i, "Scenario");
    return s.alias == null ? key : `${key} as ${atom(s.alias, /[,\r\n\t]/, "Scenario alias")}`;
  }).join(",");
  const dimensions = p.dimensions == null ? [] : list(p.dimensions, "dimensions");
  const filters: Record<string, string | string[]> = {};
  if (p.filters != null && (typeof p.filters !== "object" || Array.isArray(p.filters))) throw new Error("Unsupported filters.");
  const filterText = Object.keys(p.filters || {}).map(key => {
    if (["__proto__", "constructor", "prototype"].indexOf(key) >= 0) throw new Error("Unsupported filter key.");
    atom(key, /[=,|\r\n\t]/, "Filter key");
    const value = p.filters[key];
    const values = Array.isArray(value) ? value : [value];
    if (!values.length || (Array.isArray(value) && values.length === 1)) throw new Error("A singleton list filter cannot be preserved by the worksheet grammar.");
    const strings = values.map(v => atom(v, /[,|\r\n\t]/, "Filter value"));
    filters[key] = Array.isArray(value) ? strings : strings[0];
    return `${key}=${strings.join("|")}`;
  }).join(",");
  const scale = presentation?.scale ?? p.scale;
  const decimals = presentation ? presentation.decimals : p.decimals;
  if (typeof scale !== "number" || [0, 3, 6, 9].indexOf(scale) < 0) throw new Error("Unknown effective extract scale. Regenerate the extract.");
  if (!Object.prototype.hasOwnProperty.call(presentation || p, "decimals")) throw new Error("Unknown effective decimal settings. Regenerate the extract.");
  if (decimals !== null && (typeof decimals !== "number" || !Number.isInteger(decimals) || decimals < 0 || decimals > 15)) throw new Error("Unsupported extract decimal settings.");
  if (presentation && ((p.scale != null && p.scale !== scale) || (Object.prototype.hasOwnProperty.call(p, "decimals") && p.decimals !== decimals))) throw new Error("Conflicting extract presentation metadata.");
  const resolvedDecimals = decimals ?? 1;
  const args: Record<string, unknown> = { period_start: p.period_start, period_end: p.period_end };
  const statement = source.tool === "run_statement";
  const selection = statement ? atom(p.statement, /[\r\n\t]/, "Statement") : list(p.metrics, "metrics").join(",");
  if (!selection) throw new Error("Missing metric selection.");
  if (statement) args.statement = p.statement; else args.metrics = p.metrics;
  args.scenarios = p.scenarios;
  if (dimensions.length) args.dimensions = dimensions;
  if (Object.keys(filters).length) args.filters = filters;
  args.scale = scale;
  args.decimals = resolvedDecimals;
  const common = statement ? [scenarioText, dimensions.join(","), filterText] : [dimensions.join(","), scenarioText, filterText];
  const params = [selection, p.period_start, p.period_end, ...common].map(quote);
  if (!statement) params.push(quote(layout));
  params.push(String(scale), String(resolvedDecimals));
  if (statement) params.push(quote(layout));
  return { tool: layout === "report" ? source.tool : statement ? "run_statement_data" : "run_metric_data", args,
    formula: `=PRECIS.${statement ? "STATEMENT" : "METRIC"}(${params.join(",")})` };
}

export function retireStaticRegion(meta: any, sheet: string, rangeName: string, bounds: Bounds, state: "live" | "recovery" = "live"): any {
  const copy = JSON.parse(JSON.stringify(meta));
  const entry = copy.sheets[sheet];
  if (!entry || !Array.isArray(entry.regions)) throw new Error("Static region metadata changed. Try again.");
  const target = entry.regions.find((r: any) => r.range_name === rangeName);
  if (!target || target.state === "live") throw new Error("Static region metadata changed. Try again.");
  const live = { range_name: rangeName, state, layout: target.layout || "extract", bounds,
    origin: target.origin || copy.origin, named_ranges: target.named_ranges || [], footer_rows: 1 };
  entry.regions = entry.regions.map((r: any) => r.range_name === rangeName ? live : r);
  // A sheet-level legacy source must never overwrite the activated region.
  for (const key of ["source", "shape", "refresh_policy", "excel_tables", "named_ranges"]) delete entry[key];
  entry.kind = "user";
  const json = JSON.stringify(copy);
  if (json.length > 32767) throw new Error("Updated metadata exceeds Excel's cell limit.");
  return copy;
}
