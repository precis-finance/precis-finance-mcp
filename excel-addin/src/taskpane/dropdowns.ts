/*
 * Member drop-downs. The panel inserts a native Excel data-validation
 * drop-down for a dimension, backed by a live list on a hidden sheet:
 *
 *   - `PrecisLists` (hidden) holds one column block per provisioned dimension:
 *     a `=PRECIS.HIERARCHY(dim, "", "list")` spill ([code, display name]) plus
 *     a concat column ("code | name" — code first, so extraction is a
 *     delimiter-safe TEXTBEFORE whatever the display name contains).
 *   - Workbook-scoped defined names slice the spill per column and double as
 *     the provisioning registry: if the names exist the list is reused, so any
 *     number of drop-downs for one dimension share a single spill and the task
 *     pane's Refresh (full rebuild) updates them all in one recalc.
 *   - The validation rule references the name; a companion formula in the
 *     adjacent column resolves the other representation (name for a code
 *     drop-down, code for a "code | name" one). Never overwrites content.
 */

/* global Excel */

import { getMcpUrl, getToken } from "../config";
import { callTool } from "../mcp";

const SHEET_NAME = "PrecisLists";
const NAME_PREFIX = "Precis_List_";
/** Columns per dimension block: spill (code, name), concat, spacer. */
const BLOCK_WIDTH = 4;

export type DropdownMode = "code" | "label";

export interface DimensionEntry {
  key: string;
  label: string;
  kind: "leaf" | "derived" | "ragged";
  leaf_dimension?: string;
}

/** Fetch the dimension catalogue for the picker (metadata only). */
export async function fetchDimensions(): Promise<DimensionEntry[]> {
  const mcpUrl = getMcpUrl();
  const token = getToken();
  if (!mcpUrl || !token) {
    throw new Error("Sign in first.");
  }
  const res = await callTool<{ dimensions?: DimensionEntry[] }>(
    mcpUrl,
    token,
    "list_dimensions",
    {}
  );
  return res.dimensions ?? [];
}

function codeName(dim: string): string {
  return `${NAME_PREFIX}${dim}_code`;
}
function displayName(dim: string): string {
  return `${NAME_PREFIX}${dim}_name`;
}
function labelName(dim: string): string {
  return `${NAME_PREFIX}${dim}_label`;
}

/** 0-based column index → A1 letters (0 → A, 26 → AA). */
export function colLetter(index: number): string {
  let s = "";
  let n = index;
  for (;;) {
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26) - 1;
    if (n < 0) {
      return s;
    }
  }
}

/** The hidden list sheet, created (and hidden) on first use. */
async function ensureSheet(context: Excel.RequestContext): Promise<Excel.Worksheet> {
  const existing = context.workbook.worksheets.getItemOrNullObject(SHEET_NAME);
  existing.load("isNullObject");
  await context.sync();
  if (!existing.isNullObject) {
    return existing;
  }
  const created = context.workbook.worksheets.add(SHEET_NAME);
  created.visibility = Excel.SheetVisibility.hidden;
  return created;
}

/**
 * Provision `dim`'s list block + defined names unless already present.
 * Returns true when a new block was written.
 */
async function ensureList(
  context: Excel.RequestContext,
  sheet: Excel.Worksheet,
  dim: string
): Promise<boolean> {
  const existing = context.workbook.names.getItemOrNullObject(codeName(dim));
  existing.load("isNullObject");
  await context.sync();
  if (!existing.isNullObject) {
    return false;
  }

  const used = sheet.getUsedRangeOrNullObject();
  used.load(["isNullObject", "columnIndex", "columnCount"]);
  await context.sync();
  let startCol = 0;
  if (!used.isNullObject) {
    const firstFree = used.columnIndex + used.columnCount;
    startCol = Math.ceil(firstFree / BLOCK_WIDTH) * BLOCK_WIDTH;
  }

  const spill = colLetter(startCol);
  const concat = colLetter(startCol + 2);
  const anchor = `${SHEET_NAME}!$${spill}$2`;
  sheet.getCell(0, startCol).values = [[dim]];
  sheet.getCell(1, startCol).formulas = [[`=PRECIS.HIERARCHY("${dim}","","list")`]];
  sheet.getCell(1, startCol + 2).formulas = [
    [`=INDEX($${spill}$2#,0,1)&" | "&INDEX($${spill}$2#,0,2)`],
  ];
  context.workbook.names.add(codeName(dim), `=INDEX(${anchor}#,0,1)`);
  context.workbook.names.add(displayName(dim), `=INDEX(${anchor}#,0,2)`);
  context.workbook.names.add(labelName(dim), `=${SHEET_NAME}!$${concat}$2#`);
  await context.sync();
  return true;
}

/**
 * Write the companion formula in the column right of `sel` — display name for
 * a code drop-down, extracted code for a "code | name" one. Skips multi-column
 * selections and never overwrites a non-empty cell.
 */
async function writeCompanion(
  context: Excel.RequestContext,
  sel: Excel.Range,
  dim: string,
  mode: DropdownMode
): Promise<InsertResult["companion"]> {
  const comp = sel.getOffsetRange(0, 1);
  comp.load("values");
  await context.sync();
  const empty = comp.values.every((row) => row[0] === "" || row[0] === null);
  if (!empty) {
    return "occupied";
  }
  const formula =
    mode === "code"
      ? `=IF(RC[-1]="","",XLOOKUP(RC[-1],${codeName(dim)},${displayName(dim)},""))`
      : `=IF(RC[-1]="","",TEXTBEFORE(RC[-1]," | "))`;
  comp.formulasR1C1 = comp.values.map(() => [formula]);
  await context.sync();
  return "written";
}

export interface InsertResult {
  provisioned: boolean;
  companion: "written" | "occupied" | "skipped-multi-column";
}

/**
 * Insert a member drop-down for `dim` on the currently selected range.
 * Provisions the hidden list (spill + names) on first use, applies the
 * validation rule, and writes the companion formula next to the selection
 * when that column is free.
 */
export async function insertDropdown(dim: string, mode: DropdownMode): Promise<InsertResult> {
  let provisioned = false;
  let companion: InsertResult["companion"] = "skipped-multi-column";
  await Excel.run(async (context) => {
    const sel = context.workbook.getSelectedRange();
    sel.load(["rowCount", "columnCount", "worksheet/name"]);
    await context.sync();
    if (sel.worksheet.name === SHEET_NAME) {
      throw new Error("Select a range outside the Précis list sheet.");
    }

    const sheet = await ensureSheet(context);
    provisioned = await ensureList(context, sheet, dim);

    const source = mode === "code" ? codeName(dim) : labelName(dim);
    sel.dataValidation.clear();
    sel.dataValidation.rule = {
      list: { inCellDropDown: true, source: `=${source}` },
    };
    await context.sync();

    if (sel.columnCount === 1) {
      companion = await writeCompanion(context, sel, dim, mode);
    }
  });
  return { provisioned, companion };
}
