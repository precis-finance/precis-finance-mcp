/**
 * MCP inspection-grid widget. Binds to the `inspection_grid` block emitted by
 * `inspect_rows(out='render')` — `columns` (keys) + up to 50 `rows` + a
 * truncation note. Self-contained (no @/lib, no shadcn/Tailwind, no icons):
 * a filterable, click-to-sort table on the shared `useWidgetApp` bridge. The
 * block is pre-capped server-side, so there's no pagination here yet (server-
 * side pagination via callServerTool is the noted future enhancement).
 */
import { useMemo, useState } from "react";
import { useWidgetApp, useSyncDocumentHeight } from "../shared/host";

interface GridCaption {
  description?: string;
  scenarios?: string[];
  period?: string;
  filters?: Record<string, unknown>;
  dimensions?: string[];
  scale?: string;
  generated_at?: string;
}

interface GridBlock {
  title?: string;
  source_key?: string;
  columns?: string[];
  rows?: Record<string, unknown>[];
  row_count?: number;
  limit?: number;
  truncated?: boolean;
  caption?: GridCaption;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function captionParts(cap?: GridCaption): string[] {
  if (!cap) return [];
  const parts: string[] = [];
  if (cap.scenarios?.length) parts.push(cap.scenarios.join(" · "));
  if (cap.period) parts.push(cap.period);
  if (cap.dimensions?.length) parts.push(cap.dimensions.join(", "));
  if (cap.scale) parts.push(cap.scale);
  if (cap.generated_at) parts.push(`Data as at ${cap.generated_at}`);
  return parts;
}

export function InspectionGrid() {
  const { structuredContent, error } = useWidgetApp("precis-inspection-grid");
  useSyncDocumentHeight();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ col: string; dir: "asc" | "desc" } | null>(null);

  const block = structuredContent as GridBlock;
  const columns = block.columns ?? [];
  const allRows = block.rows ?? [];

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let r = needle
      ? allRows.filter((row) =>
          columns.some((c) => formatCell(row[c]).toLowerCase().includes(needle)),
        )
      : allRows;
    if (sort) {
      r = [...r].sort((a, b) => {
        const cmp = formatCell(a[sort.col]).localeCompare(
          formatCell(b[sort.col]),
          undefined,
          { numeric: true, sensitivity: "base" },
        );
        return sort.dir === "asc" ? cmp : -cmp;
      });
    }
    return r;
  }, [allRows, columns, query, sort]);

  if (error) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        Unable to connect to the host: {error.message}
      </div>
    );
  }
  if (!columns.length) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        No rows to display.
      </div>
    );
  }

  const caption = captionParts(block.caption);

  function toggleSort(col: string) {
    setSort((p) =>
      !p || p.col !== col
        ? { col, dir: "asc" }
        : p.dir === "asc"
          ? { col, dir: "desc" }
          : null,
    );
  }

  return (
    <div style={{ padding: "8px", fontFamily: "var(--w-font-sans)" }}>
      <div style={{ fontWeight: 500, marginBottom: "2px" }}>
        {block.title || block.source_key || "Inspection"}
      </div>
      <div style={{ color: "var(--w-ink-faint)", fontSize: "12px", marginBottom: "6px" }}>
        {(block.row_count ?? allRows.length).toLocaleString()} rows
        {block.limit ? ` · limit ${block.limit.toLocaleString()}` : ""}
        {block.truncated ? " · truncated" : ""}
        {caption.length ? " · " + caption.join(" · ") : ""}
      </div>
      {block.truncated && (
        <div
          style={{
            marginBottom: "8px",
            padding: "6px 8px",
            color: "var(--w-ink-muted)",
            background: "var(--w-surface-muted)",
            border: "1px solid var(--w-border)",
            borderRadius: "var(--w-radius)",
            fontSize: "12px",
          }}
        >
          This widget shows only the first {allRows.length.toLocaleString()} preview rows.
          Refine the inspection filters for a narrower result.
        </div>
      )}
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Filter rows"
        style={{
          width: "100%",
          boxSizing: "border-box",
          marginBottom: "8px",
          padding: "6px 8px",
          fontSize: "12px",
          fontFamily: "var(--w-font-sans)",
          color: "var(--w-ink)",
          background: "transparent",
          border: "1px solid var(--w-border)",
          borderRadius: "var(--w-radius)",
        }}
      />
      <div style={{ maxHeight: "420px", overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c}
                  onClick={() => toggleSort(c)}
                  style={{
                    padding: "6px 8px",
                    textAlign: "left",
                    color: "var(--w-ink-muted)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    cursor: "pointer",
                    position: "sticky",
                    top: 0,
                    background: "var(--w-surface-muted)",
                    borderBottom: "1px solid var(--w-border)",
                  }}
                >
                  {c}
                  {sort?.col === c ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} style={{ borderBottom: "1px solid var(--w-border)" }}>
                {columns.map((c) => (
                  <td
                    key={c}
                    style={{
                      padding: "4px 8px",
                      whiteSpace: "nowrap",
                      fontFamily:
                        typeof row[c] === "number"
                          ? "var(--w-font-mono)"
                          : "var(--w-font-sans)",
                      textAlign: typeof row[c] === "number" ? "right" : "left",
                    }}
                  >
                    {formatCell(row[c])}
                  </td>
                ))}
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={Math.max(columns.length, 1)}
                  style={{ padding: "24px", textAlign: "center", color: "var(--w-ink-muted)" }}
                >
                  No rows match the filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
