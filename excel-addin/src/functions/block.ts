/*
 * Financial format types, source-text sanitization and discovery grids.
 * Financial spill grids come from the server's shared worksheet projection.
 */

export interface FinancialColumn {
  key: string;
  label?: string;
  format?: string;
  decimals?: number;
  nf?: string;
  variance?: boolean;
  group?: string;
}

export interface FinancialRow {
  row_type?: string;
  label?: string;
  indent?: number;
  values?: Record<string, number | null>;
  alerts?: Record<string, string>;
  // Legacy per-row fallback; cell_nf resolves row/scenario precedence on server.
  nf?: string;
  cell_nf?: Record<string, string>;
}

export interface FinancialTableBlock {
  type: string;
  title?: string;
  columns: FinancialColumn[];
  rows: FinancialRow[];
}

/*
 * Formula-injection guard (OWASP CSV-injection class). A string cell that begins
 * with =, +, -, @, or a leading control char is parsed as a formula when it
 * re-enters Excel's cell parser — on Paste-Special→Values out of the spill, or
 * on CSV export and reopen. Custom-function spill results are rendered as literal
 * text (not re-evaluated), so for the live spill this is defence-in-depth; it
 * closes the downstream re-entry paths for every externally-sourced string
 * (dimension members, statement/metric labels, scenario aliases) before it lands
 * in a cell. The standard "'" text-qualifier prefix neutralises the trigger and
 * is hidden by Excel on re-entry. Numbers are never touched — figures spill as
 * real numbers, so negatives stay numeric.
 */
const FORMULA_TRIGGER = /^[=+\-@\t\r]/;

function sanitizeCell(v: string | number): string | number {
  return typeof v === "string" && FORMULA_TRIGGER.test(v) ? `'${v}` : v;
}

/** Apply the formula-injection guard to every string cell in a spill matrix. */
export function sanitizeGrid(grid: (string | number)[][]): (string | number)[][] {
  return grid.map((row) => row.map(sanitizeCell));
}

/*
 * Generic "list of records → flat spill" for the discovery tools (search_hierarchy
 * sections, list_kpis): a header row of the union of keys (first-seen order), then
 * one row per record. Arrays are joined; objects are JSON-stringified; null → "".
 */
function cellValue(v: unknown): string | number {
  if (v == null) {
    return "";
  }
  if (typeof v === "number" || typeof v === "string") {
    return v;
  }
  if (typeof v === "boolean") {
    return v ? "TRUE" : "FALSE";
  }
  if (Array.isArray(v)) {
    return v.map((x) => String(x)).join(", ");
  }
  return JSON.stringify(v);
}

export function objectsToGrid(rows: Record<string, unknown>[]): (string | number)[][] {
  if (!rows || rows.length === 0) {
    return [["(no results)"]];
  }
  const cols: string[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    for (const k of Object.keys(r ?? {})) {
      if (!seen.has(k)) {
        seen.add(k);
        cols.push(k);
      }
    }
  }
  const grid: (string | number)[][] = [cols];
  for (const r of rows) {
    grid.push(cols.map((c) => cellValue((r ?? {})[c])));
  }
  return sanitizeGrid(grid);
}
