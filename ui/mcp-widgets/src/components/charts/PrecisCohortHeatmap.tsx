/**
 * PrecisCohortHeatmap — numbers-in-cells grid with colour intensity (MCP widget
 * port). Long-format data: rowKey = row dim, colKey = column dim, valueKey =
 * cell. Cell background is teal at an opacity scaled across the value range.
 */

import { useMemo } from "react";
import { buildFormatY } from "./format";

export interface PrecisCohortHeatmapProps {
  data: Record<string, unknown>[];
  rowKey: string;
  colKey: string;
  valueKey: string;
  valueFormat?: string;
  decimals?: number;
}

function asNumber(val: unknown): number | null {
  if (typeof val === "number" && !isNaN(val)) return val;
  if (typeof val === "string") {
    const n = parseFloat(val);
    return isNaN(n) ? null : n;
  }
  return null;
}

function cellBackground(intensity: number): string {
  const weight = 8 + intensity * 22;
  return `color-mix(in srgb, var(--w-chart-1) ${weight.toFixed(1)}%, var(--w-surface-solid))`;
}

export function PrecisCohortHeatmap({
  data,
  rowKey,
  colKey,
  valueKey,
  valueFormat = "number",
  decimals,
}: PrecisCohortHeatmapProps) {
  const fmt = useMemo(
    () => buildFormatY(valueFormat, 0, decimals),
    [valueFormat, decimals],
  );

  const { rows, cols, cells, min, max } = useMemo(() => {
    const rowOrder: string[] = [];
    const colOrder: string[] = [];
    const rowSeen = new Set<string>();
    const colSeen = new Set<string>();
    const cellMap = new Map<string, number>();
    let lo = Infinity;
    let hi = -Infinity;

    for (const d of data) {
      const r = String(d[rowKey] ?? "");
      const c = String(d[colKey] ?? "");
      if (!rowSeen.has(r)) {
        rowSeen.add(r);
        rowOrder.push(r);
      }
      if (!colSeen.has(c)) {
        colSeen.add(c);
        colOrder.push(c);
      }
      const v = asNumber(d[valueKey]);
      if (v !== null) {
        cellMap.set(`${r} ${c}`, v);
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    return { rows: rowOrder, cols: colOrder, cells: cellMap, min: lo, max: hi };
  }, [data, rowKey, colKey, valueKey]);

  if (rows.length === 0 || cols.length === 0) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        No data to display.
      </div>
    );
  }

  const range = max - min;
  const intensityOf = (v: number) => (range > 0 ? (v - min) / range : 0.5);

  return (
    <div style={{ overflowX: "auto" }}>
      <table
        style={{
          borderCollapse: "collapse",
          fontFamily: "var(--w-font-mono)",
          fontSize: 13,
        }}
      >
        <thead>
          <tr>
            <th style={{ padding: "4px 8px" }} />
            {cols.map((c) => (
              <th
                key={c}
                style={{
                  padding: "4px 12px",
                  textAlign: "right",
                  fontWeight: 500,
                  color: "var(--w-ink-muted)",
                  fontSize: 11,
                }}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r}>
              <th
                style={{
                  padding: "4px 8px",
                  textAlign: "left",
                  fontWeight: 500,
                  whiteSpace: "nowrap",
                  color: "var(--w-ink-muted)",
                  fontSize: 11,
                }}
              >
                {r}
              </th>
              {cols.map((c) => {
                const v = cells.get(`${r} ${c}`);
                if (v === undefined) {
                  return <td key={c} style={{ padding: "4px 12px" }} />;
                }
                const intensity = intensityOf(v);
                return (
                  <td
                    key={c}
                    style={{
                      padding: "4px 12px",
                      textAlign: "right",
                      background: cellBackground(intensity),
                      color: "var(--w-ink)",
                    }}
                  >
                    {fmt(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
