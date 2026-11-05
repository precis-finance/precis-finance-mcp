import { McpEnvelope, callToolEnvelope } from "./mcp";
import { readSignature, takePreparedRead, setWorksheetFormats } from "./config";

export interface WorksheetProjection {
  version: number;
  grid: (string | number)[][];
  number_formats: string[][];
  key_columns: string[];
  value_columns: string[];
  column_keys?: string[];
  header_rows: number;
  data_rows: number;
  footer_rows: number;
}

export function worksheetProjection(envelope: McpEnvelope, layout: "report" | "extract" = "extract"): WorksheetProjection {
  const root = envelope._meta?.["precis/worksheet"];
  const projection = (layout === "report" ? root?.report : root) as WorksheetProjection;
  if (projection?.version !== 1 || !Array.isArray(projection.grid) || !Array.isArray(projection.number_formats)) {
    throw new Error("This server does not support full-precision worksheet reads. Update Précis before converting.");
  }
  if (projection.footer_rows !== 1 || !Number.isInteger(projection.data_rows) || projection.data_rows < 1 ||
      ![1, 2].includes(projection.header_rows) || (layout === "extract" && projection.header_rows !== 1) ||
      projection.grid.length !== projection.header_rows + projection.data_rows + 1 ||
      projection.number_formats.length !== projection.grid.length) {
    throw new Error("The server did not return a supported financial footer. Update Précis before converting.");
  }
  return projection;
}

export async function financialRead(url: string, token: string, tool: string, args: Record<string, unknown>, anchor?: string): Promise<McpEnvelope> {
  const cached = anchor && takePreparedRead(anchor, readSignature(tool, args));
  const envelope = cached as McpEnvelope || await callToolEnvelope(url, token, tool, args, true);
  const projection = worksheetProjection(envelope);
  if (anchor) setWorksheetFormats(anchor, tool.endsWith("_data") ? projection.number_formats : undefined);
  return envelope;
}
