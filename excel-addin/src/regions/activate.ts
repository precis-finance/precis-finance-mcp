/* global Excel, Office */
import { getMcpUrl, getToken, prepareRead, discardPreparedRead, readSignature } from "../config";
import { financialRead, worksheetProjection } from "../worksheet";
import { sanitizeGrid, FinancialTableBlock } from "../functions/block";
import { applyBlockFormat, applyFooterFormat } from "../format";
import { Bounds, inside, overlaps, sameGrid, extractDefinition, parseMeta, retireStaticRegion } from "./contract";

const properties = ["rowIndex", "columnIndex", "rowCount", "columnCount", "address", "values", "formulas"];
const bounds = (r: Excel.Range): Bounds => ({ row: r.rowIndex, column: r.columnIndex, rows: r.rowCount, columns: r.columnCount });
const fingerprint = (r: Excel.Range): string => JSON.stringify([r.address, r.values, r.formulas]);
const sameBounds = (a: Bounds, b: Bounds): boolean => inside(a, b) && inside(b, a);
const quotedSheet = (name: string): string => `'${name.replace(/'/g, "''")}'`;
let busy = false;

interface FooterGeometry {
  row: Excel.Range;
  height: number;
  standard: boolean;
  applied?: boolean;
}

async function captureFooterGeometry(sheet: Excel.Worksheet, owned: Bounds, destination: Bounds, context: Excel.RequestContext): Promise<FooterGeometry[]> {
  const indices = new Set([destination.row + destination.rows - 1, owned.row + owned.rows - 1]);
  const ranges = Array.from(indices).map(index => {
    // Only the two potentially affected rows; no workbook formula discovery.
    const row = sheet.getRangeByIndexes(index, 0, 1, 16384);
    row.load("rowIndex");
    row.format.load(["rowHeight", "useStandardHeight"]);
    return row;
  });
  await context.sync();
  return ranges.map(row => ({ row, height: row.format.rowHeight, standard: row.format.useStandardHeight }));
}

function applyFooterGeometry(rows: FooterGeometry[], extent: Bounds): void {
  for (const saved of rows) {
    const values = saved.row.values[0], formulas = saved.row.formulas[0];
    const shared = values.some((value, column) =>
      (column < extent.column || column >= extent.column + extent.columns) &&
      (value !== "" && value != null || typeof formulas[column] === "string" && formulas[column].startsWith("=")));
    if (!shared) {
      saved.applied = true;
      saved.row.format.useStandardHeight = true;
    }
  }
}

async function removeRecovery(backup: Excel.Worksheet, context: Excel.RequestContext, name: string): Promise<string> {
  try {
    // Excel forbids deleting VeryHidden sheets. Commit visibility first so
    // cleanup works on both web and desktop hosts.
    backup.visibility = Excel.SheetVisibility.hidden;
    await context.sync();
    backup.delete();
    await context.sync();
    return "";
  } catch (error) {
    try { backup.visibility = Excel.SheetVisibility.visible; await context.sync(); } catch { /* retain recovery data */ }
    return ` Recovery-sheet cleanup could not be confirmed for ${name}: ${String(error)} Check this sheet for the original cells.`;
  }
}

async function mergedBounds(range: Excel.Range, context: Excel.RequestContext): Promise<Bounds[]> {
  const merged = range.getMergedAreasOrNullObject();
  merged.load("isNullObject");
  await context.sync(); // Excel rejects more than 512 merged areas before any mutation.
  if (merged.isNullObject) return [];
  merged.areas.load("items/rowIndex,items/columnIndex,items/rowCount,items/columnCount");
  await context.sync();
  return merged.areas.items.map(bounds).sort((a, b) => a.row - b.row || a.column - b.column);
}

export function activationSupported(): boolean {
  // Merged-area inspection is 1.13; spill APIs are 1.12. The add-in's other
  // functions retain their existing host floor.
  return Office.context.requirements.isSetSupported("ExcelApi", "1.13");
}

/** One selected generated export; no workbook formula scan or full calculation. */
export async function activateSelectedExtract(status: (message: string) => void): Promise<string> {
  if (busy) throw new Error("A conversion is already running.");
  if (!activationSupported()) throw new Error("Conversion requires ExcelApi 1.13 or later. The extract has not changed.");
  const token = getToken(), url = getMcpUrl();
  if (!token || !url) throw new Error("Sign in before converting an extract.");
  busy = true;
  try {
    return await Excel.run(async context => {
      const workbook = context.workbook;
      const selection = workbook.getSelectedRange(); // Excel rejects discontiguous selections.
      const sheet = selection.worksheet;
      const metaSheet = workbook.worksheets.getItemOrNullObject("_precis_meta");
      selection.load(properties.slice(0, 5));
      sheet.load(["name", "id"]);
      sheet.protection.load("protected");
      metaSheet.load("isNullObject");
      await context.sync();
      if (sheet.protection.protected) throw new Error("Unprotect the selected sheet before converting.");
      if (metaSheet.isNullObject) throw new Error("No Précis provenance found. Select a generated extract or regenerate it.");
      const metaCell = metaSheet.getRange("A1");
      metaCell.load("values");
      await context.sync();
      const rawMeta = String(metaCell.values[0][0] || "");
      const meta = parseMeta(rawMeta);
      const entry = meta.sheets[sheet.name];
      if (!entry || entry.kind === "roundtrip" || meta.workbook_kind === "roundtrip") throw new Error("Planning templates and arbitrary tables cannot be converted.");
      const tables = sheet.tables;
      tables.load("items/name");
      await context.sync();
      const tableRanges = tables.items.map(table => {
        table.load(["name", "style", "showTotals", "showHeaders"]);
        const range = table.getRange();
        range.load(properties.slice(0, 5));
        return { table, range };
      });
      await context.sync();
      const regions = entry.regions;
      if (!regions) throw new Error("This export has no reliable region boundary. Regenerate it.");
      if (!Array.isArray(regions) || regions.length > 200) throw new Error("Invalid or oversized region metadata.");
      const candidates: { region: any; range: Excel.Range; name?: Excel.NamedItem }[] = [];
      for (const region of regions) {
        if (typeof region.range_name !== "string" || !region.range_name) throw new Error("Invalid region boundary name.");
        const name = workbook.names.getItemOrNullObject(region.range_name);
        name.load(["isNullObject", "formula"]);
        await context.sync();
        if (name.isNullObject) throw new Error(`Missing region boundary ${region.range_name}. Regenerate the extract.`);
        const range = name.getRangeOrNullObject();
        range.load(["isNullObject", ...properties.slice(0, 5)]);
        range.worksheet.load("id");
        await context.sync();
        if (range.isNullObject || range.worksheet.id !== sheet.id) throw new Error("Broken or misplaced region boundary.");
        candidates.push({ region, range, name });
      }
      const matches = candidates.filter(c => inside(bounds(selection), bounds(c.range)));
      if (matches.length !== 1) throw new Error("Select cells wholly inside one generated Précis extract.");
      const target = matches[0], region = target.region;
      const old = target.range, owned = bounds(old);
      const originalRegionFormula = target.name?.formula;
      if (owned.rows * owned.columns > 100000) throw new Error("This extract exceeds the 100,000-cell conversion limit.");
      old.load(properties);
      await context.sync();
      if (region.state && region.state !== "static" || region.layout !== "extract" && region.layout !== "report") throw new Error("Select a generated export with report or extract layout. Live/recovery regions cannot be activated.");
      const report = region.layout === "report";
      const footerRows = 1;
      if (region.shape?.footer_rows !== 1) throw new Error("Unsupported report footer geometry. Regenerate the export.");
      let headers: Excel.Range | undefined;
      if (report) {
        // The managed region begins at its column headers; the title is outside it.
        const shape = region.shape, offset = shape?.data_start_row - 1;
        if (!Number.isInteger(shape?.data_start_row) || !Number.isInteger(shape?.header_row) ||
            shape.header_row !== 1 || ![1, 2].includes(shape.data_start_row - shape.header_row) ||
            offset < 1 || offset >= owned.rows || !Number.isInteger(shape.row_count) ||
            offset + shape.row_count + footerRows !== owned.rows || !Array.isArray(shape.column_keys) ||
            shape.column_keys.length !== owned.columns) throw new Error("This report has no reliable data-start boundary. Regenerate it.");
        headers = sheet.getRangeByIndexes(owned.row + shape.header_row - 1, owned.column,
          shape.data_start_row - shape.header_row, owned.columns);
        headers.load(properties);
        await context.sync();
      }
      const origin = region.origin || meta.origin;
      const connectedOrigin = url.replace(/\/mcp\/?$/, "").replace(/\/+$/, "");
      if (typeof origin !== "string" || !origin) throw new Error("This extract has no recorded Précis instance. Regenerate it with PRECIS_BASE_URL configured before converting.");
      if (origin.replace(/\/+$/, "") !== connectedOrigin) throw new Error("This extract came from a different Précis instance. Connect to its originating instance before converting.");
      if (region.original_formula || region.formula) throw new Error("Restoring stored formula references is not supported yet. Regenerate this extract.");
      const generated = report ? undefined : tableRanges.find(t => t.table.name === region.table_name);
      const tableBounds = { ...owned, rows: owned.rows - footerRows };
      if (!report && (!generated || !sameBounds(bounds(generated.range), tableBounds))) throw new Error("The generated Table and region boundary no longer agree. Regenerate the extract.");
      if (generated && (generated.table.showTotals || !generated.table.showHeaders)) throw new Error("Tables with custom totals or hidden headers are not supported.");
      const formulas = old.getSpecialCellsOrNullObject(Excel.SpecialCellType.formulas);
      formulas.load("isNullObject");
      const slicers = sheet.slicers;
      slicers.load("items/name");
      await context.sync();
      if (!formulas.isNullObject) throw new Error("The extract contains calculated columns or user formulas. Preserve them before conversion.");
      if (generated && slicers.items.length) throw new Error("Remove or relocate slicers from this sheet before converting its Table.");
      const definition = extractDefinition(region.source, region.presentation, region.layout);
      const originalFingerprint = fingerprint(old);
      status(`Reading fresh data for ${generated?.table.name || old.address}…`);
      const envelope = await financialRead(url, token, definition.tool, definition.args);
      const projection = worksheetProjection(envelope, region.layout), expected = sanitizeGrid(projection.grid);
      if (headers) {
        const full = worksheetProjection(envelope, "report");
        const labels = full.grid.slice(0, full.header_rows).map(row => row.slice());
        // Static grouped headers put the label in the vertically merged top
        // cell; the live projection puts it on the bottom header row.
        if (full.header_rows === 2) { labels[0][0] = labels[1][0]; labels[1][0] = ""; }
        if (!sameGrid(headers.values, labels)) throw new Error("Report headers changed or no longer match the query columns. Regenerate the export before converting.");
      }
      const block = report ? envelope._meta?.["precis/renderBlock"] as FinancialTableBlock : undefined;
      if (report && !block) throw new Error("Précis returned no report block. The export has not changed.");
      if (expected.length * expected[0].length > 100000) throw new Error("This conversion exceeds the 100,000-cell limit. Reduce the extract.");
      const destination: Bounds = { ...owned, rows: expected.length, columns: expected[0].length };
      if (destination.row + destination.rows > 1048576 || destination.column + destination.columns > 16384) throw new Error("The spill would exceed the worksheet boundary.");
      const extent: Bounds = { ...owned, rows: Math.max(owned.rows, destination.rows), columns: Math.max(owned.columns, destination.columns) };
      if (extent.rows * extent.columns > 100000) throw new Error("The replacement and recovery area exceeds the 100,000-cell limit.");
      const footerGeometry = await captureFooterGeometry(sheet, owned, destination, context);
      const replacement = sheet.getRangeByIndexes(extent.row, extent.column, extent.rows, extent.columns);
      replacement.load(properties.concat("hasSpill"));
      old.load(properties);
      metaCell.load("values");
      generated?.range.load(properties);
      await context.sync();
      if (getToken() !== token || String(metaCell.values[0][0]) !== rawMeta || fingerprint(old) !== originalFingerprint ||
          generated && !sameBounds(bounds(generated.range), tableBounds)) throw new Error("The extract or session changed while reading. Try again.");
      const merges = await mergedBounds(replacement, context);
      if (replacement.hasSpill !== false || merges.some(m => !headers || !inside(m, bounds(headers)))) throw new Error("A merge outside the report column headers or an existing spill obstructs conversion. The export has not changed.");
      for (let r = 0; r < extent.rows; r++) for (let c = 0; c < extent.columns; c++) {
        if (r < owned.rows && c < owned.columns) continue;
        if (replacement.values[r][c] !== "" && replacement.values[r][c] != null ||
            typeof replacement.formulas[r][c] === "string" && replacement.formulas[r][c].startsWith("=")) throw new Error("The new spill would overwrite neighbouring content.");
      }
      if (tableRanges.some(t => t !== generated && overlaps(bounds(t.range), extent)) || candidates.some(c => c !== target && overlaps(bounds(c.range), extent))) throw new Error("The replacement overlaps another Table or Précis region.");
      const shape = region.shape;
      if (!shape || (report ? !Array.isArray(shape.column_keys) || JSON.stringify(shape.column_keys) !== JSON.stringify(projection.column_keys) : JSON.stringify(shape.key_columns) !== JSON.stringify(projection.key_columns) || JSON.stringify(shape.value_columns) !== JSON.stringify(projection.value_columns))) throw new Error("Export columns changed. Regenerate it before conversion.");
      const named = [] as { item: Excel.NamedItem; formula: string; column: number }[];
      const seenNames = new Set<string>();
      if (!report && (!Array.isArray(region.named_ranges) || region.named_ranges.length !== projection.value_columns.length)) throw new Error("Missing generated value-column name metadata. Regenerate the extract.");
      if (report && region.named_ranges?.length) throw new Error("Custom report dependency names cannot be remapped automatically. Preserve them before converting.");
      for (let i = 0; i < (region.named_ranges || []).length; i++) {
        const record = region.named_ranges[i];
        if (typeof record.name !== "string" || seenNames.has(record.name.toUpperCase()) || record.name.toUpperCase() === region.range_name.toUpperCase()) throw new Error("Duplicate or invalid value-column name metadata.");
        seenNames.add(record.name.toUpperCase());
        const item = workbook.names.getItemOrNullObject(record.name);
        item.load(["isNullObject", "formula"]);
        await context.sync();
        if (item.isNullObject || i >= projection.value_columns.length) throw new Error("A generated value-column name is missing or invalid.");
        const namedRange = item.getRangeOrNullObject();
        namedRange.load(["isNullObject", ...properties.slice(0, 5)]);
        namedRange.worksheet.load("id");
        await context.sync();
        const expectedNameBounds = { row: owned.row + 1, column: owned.column + projection.key_columns.length + i, rows: owned.rows - 1 - footerRows, columns: 1 };
        if (namedRange.isNullObject || namedRange.worksheet.id !== sheet.id || !sameBounds(bounds(namedRange), expectedNameBounds)) throw new Error("A value-column name was rebound outside its generated extract. Restore it before converting.");
        named.push({ item, formula: item.formula, column: projection.key_columns.length + i + 1 });
      }
      const backupName = `_PrecisRecovery_${Date.now().toString().slice(-10)}`;
      const backup = workbook.worksheets.add(backupName);
      for (const saved of footerGeometry) backup.getRangeByIndexes(saved.row.rowIndex, 0, 1, 1).format.rowHeight = saved.height;
      backup.visibility = Excel.SheetVisibility.veryHidden;
      const saved = backup.getRangeByIndexes(extent.row, extent.column, extent.rows, extent.columns);
      saved.copyFrom(replacement, Excel.RangeCopyType.all);
      await context.sync(); // Recovery exists before the first destructive operation.
      old.load(properties);
      replacement.load(properties);
      metaCell.load("values");
      for (const n of named) n.item.load("formula");
      target.name?.load("formula");
      const latestMerges = await mergedBounds(replacement, context);
      await context.sync();
      if (getToken() !== token || fingerprint(old) !== originalFingerprint || String(metaCell.values[0][0]) !== rawMeta || named.some(n => n.item.formula !== n.formula) || target.name?.formula !== originalRegionFormula || JSON.stringify(latestMerges) !== JSON.stringify(merges)) {
        const cleanup = await removeRecovery(backup, context, backupName);
        throw new Error(`The target changed during preflight. No extract cells were changed.${cleanup}`);
      }
      for (let r = 0; r < extent.rows; r++) for (let c = 0; c < extent.columns; c++) {
        if (r < owned.rows && c < owned.columns) continue;
        if (replacement.values[r][c] !== "" && replacement.values[r][c] != null || typeof replacement.formulas[r][c] === "string" && replacement.formulas[r][c].startsWith("=")) {
          const cleanup = await removeRecovery(backup, context, backupName);
          throw new Error(`Neighbouring cells changed during preflight. No extract cells were changed.${cleanup}`);
        }
      }
      const anchor = old.getCell(0, 0);
      const anchorAddress = old.address.split(":")[0];
      const tableName = generated?.table.name, tableStyle = generated?.table.style;
      let converted = false, unmerged = false, metadataQueued = false, intervened = false, metadataConflict = false;
      let committedMeta = "";
      const changedNameFormulas = new Map<Excel.NamedItem, string>();
      const regionName = region.range_name;
      const originalNameFormula = originalRegionFormula;
      try {
        // Keep one definition: the active query lives only in its formula.
        const retired = retireStaticRegion(meta, sheet.name, regionName, destination);
        const regionItem = workbook.names.getItem(regionName);
        prepareRead(anchorAddress, readSignature(definition.tool, definition.args), envelope);
        if (generated) { generated.table.convertToRange(); converted = true; }
        if (merges.length) { old.unmerge(); unmerged = true; }
        old.clear(Excel.ClearApplyTo.all);
        anchor.numberFormat = [["General"]];
        anchor.formulas = [[definition.formula]];
        anchor.calculate();
        await context.sync();
        status("Waiting for the live spill…");
        const deadline = Date.now() + 30000;
        let completed = false;
        while (Date.now() < deadline) {
          const spill = anchor.getSpillingToRangeOrNullObject();
          spill.load(["isNullObject", ...properties]);
          anchor.load(["formulas", "values"]);
          await context.sync();
          if (anchor.formulas[0][0] !== definition.formula) {
            intervened = true;
            throw new Error("The formula was edited during conversion.");
          }
          if (!spill.isNullObject && sameBounds(bounds(spill), destination)) {
            if (!sameGrid(spill.values, expected)) throw new Error("The spill differs from its prepared result. The original extract will be restored.");
            metaCell.load("values");
            for (const saved of footerGeometry) {
              saved.row.load(["values", "formulas"]);
              saved.row.format.load("rowHeight");
            }
            for (const n of named) n.item.load("formula");
            target.name?.load("formula");
            await context.sync();
            if (getToken() !== token) throw new Error("The session changed during calculation. The original export will be restored.");
            if (String(metaCell.values[0][0]) !== rawMeta || named.some(n => n.item.formula !== n.formula) || target.name?.formula !== originalRegionFormula) {
              intervened = true;
              metadataConflict = String(metaCell.values[0][0]) !== rawMeta;
              throw new Error("Workbook provenance or dependency names changed during calculation.");
            }
            if (footerGeometry.some(saved => saved.row.format.rowHeight !== saved.height)) {
              intervened = true;
              throw new Error("A footer row's height changed during conversion.");
            }
            if (block) applyBlockFormat(spill, block);
            spill.numberFormat = projection.number_formats;
            if (!block) applyFooterFormat(spill);
            applyFooterGeometry(footerGeometry, extent);
            // Name the live rectangle and value columns dynamically, excluding headers.
            const local = spill.address.split("!").pop().split(":")[0].replace(/\$/g, "");
            const ref = `${quotedSheet(sheet.name)}!${local.replace(/^([A-Z]+)(\d+)$/i, "$$$1$$$2")}#`;
            regionItem.formula = `=${ref}`;
            for (const n of named) {
              const formula = `=INDEX(${ref},2,${n.column}):INDEX(${ref},ROWS(${ref})-1,${n.column})`;
              changedNameFormulas.set(n.item, formula);
              n.item.formula = formula;
            }
            metadataQueued = true;
            committedMeta = JSON.stringify(retired);
            metaCell.values = [[committedMeta]]; // JSON is valid YAML.
            await context.sync();
            completed = true;
            break;
          }
          await new Promise(resolve => setTimeout(resolve, 200));
        }
        if (!completed) throw new Error("The formula did not produce the expected spill before the timeout.");
        // Cleanup failure must not roll back an already verified, committed spill.
        const cleanup = await removeRecovery(backup, context, backupName);
        return (report ? `Converted ${old.address} to a live report formula with its context footer. Your title was kept unchanged. Refreshed content may change data-row positions.` :
          `Converted ${tableName} to a live formula. Table references now use fixed cells; named value columns follow the spill.`) + cleanup;
      } catch (error) {
        discardPreparedRead(anchorAddress);
        // Re-read before recovery: a user can edit growth cells while a spill
        // is blocked. Never clear content whose ownership became uncertain.
        replacement.load(properties);
        const currentSpill = anchor.getSpillingToRangeOrNullObject();
        currentSpill.load(["isNullObject", ...properties.slice(0, 5)]);
        saved.load(["values", "formulas"]);
        anchor.load("formulas");
        metaCell.load("values");
        for (const saved of footerGeometry) saved.row.format.load(["rowHeight", "useStandardHeight"]);
        for (const n of named) n.item.load("formula");
        const recoveryMerges = await mergedBounds(replacement, context);
        await context.sync();
        if (recoveryMerges.length && (unmerged || JSON.stringify(recoveryMerges) !== JSON.stringify(merges))) intervened = true;
        const latestMeta = String(metaCell.values[0][0]);
        if (latestMeta !== rawMeta && latestMeta !== committedMeta) { metadataConflict = true; intervened = true; }
        if (named.some(n => n.item.formula !== n.formula && n.item.formula !== changedNameFormulas.get(n.item))) intervened = true;
        if (footerGeometry.some(saved => saved.applied && saved.row.format.rowHeight !== saved.height &&
            !saved.row.format.useStandardHeight)) intervened = true;
        if (anchor.formulas[0][0] !== definition.formula && anchor.formulas[0][0] !== saved.values[0][0]) intervened = true;
        for (let r = 0; r < extent.rows; r++) for (let c = 0; c < extent.columns; c++) {
          if (r === 0 && c === 0) continue;
          const current = replacement.values[r][c];
          const point = { row: extent.row + r, column: extent.column + c, rows: 1, columns: 1 };
          const ours = !currentSpill.isNullObject && inside(point, bounds(currentSpill));
          const formula = replacement.formulas[r][c];
          const changedFormula = typeof formula === "string" && formula.startsWith("=") && formula !== saved.values[r][c];
          if (!ours && (changedFormula || current !== "" && current != null && current !== saved.values[r][c])) intervened = true;
        }
        if (intervened) {
          // Never erase a user's competing edit to restore our own snapshot.
          backup.visibility = Excel.SheetVisibility.visible;
          if (!metadataConflict && latestMeta === rawMeta) metaCell.values = [[JSON.stringify(retireStaticRegion(meta, sheet.name, regionName, destination, "recovery"))]];
          await context.sync();
          throw new Error(`The target was edited during conversion. Changes were preserved; original extract cells are on ${backupName}. ${String(error)}`);
        }
        try {
          anchor.clear(Excel.ClearApplyTo.contents);
          old.unmerge();
          replacement.copyFrom(saved, Excel.RangeCopyType.all);
          if (converted) {
            const restored = sheet.tables.add(sheet.getRangeByIndexes(tableBounds.row, tableBounds.column, tableBounds.rows, tableBounds.columns), true);
            restored.name = tableName;
            restored.style = tableStyle;
          }
          for (const n of named) n.item.formula = n.formula;
          if (originalNameFormula) target.name.formula = originalNameFormula;
          if (metadataQueued) metaCell.values = [[rawMeta]];
          for (const saved of footerGeometry) if (saved.applied) {
            if (saved.standard) saved.row.format.useStandardHeight = true;
            else saved.row.format.rowHeight = saved.height;
          }
          await context.sync();
        } catch (recoveryError) {
          try { backup.visibility = Excel.SheetVisibility.visible; await context.sync(); } catch { /* retain recovery data */ }
          throw new Error(`Conversion failed; recovery is incomplete. Original cells are saved on ${backupName}. Conversion error: ${String(error)} Recovery error: ${String(recoveryError)}`);
        }
        const cleanup = await removeRecovery(backup, context, backupName);
        throw new Error(`Original extract restored.${generated ? " Excel's rewritten Table references may remain fixed-cell references." : ""} ${String(error)}${cleanup}`);
      } finally { discardPreparedRead(anchorAddress); }
    });
  } finally { busy = false; }
}
