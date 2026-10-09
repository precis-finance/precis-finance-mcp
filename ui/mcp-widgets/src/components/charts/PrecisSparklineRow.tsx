/**
 * PrecisSparklineRow — labelled list of sparklines for side-by-side trend
 * comparison (MCP widget port). Long-format: rowKey = category per row, xKey =
 * ordering within a row, valueKey = plotted value.
 */

import { useMemo } from "react";
import { Sparkline } from "./Sparkline";
import { getSeriesColor } from "./theme";
import { buildFormatY } from "./format";

export interface PrecisSparklineRowProps {
  data: Record<string, unknown>[];
  rowKey: string;
  xKey: string;
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

export function PrecisSparklineRow({
  data,
  rowKey,
  xKey,
  valueKey,
  valueFormat = "number",
  decimals,
}: PrecisSparklineRowProps) {
  const fmt = useMemo(
    () => buildFormatY(valueFormat, 0, decimals),
    [valueFormat, decimals],
  );

  const groups = useMemo(() => {
    const order: string[] = [];
    const byKey = new Map<string, number[]>();
    for (const d of data) {
      const k = String(d[rowKey] ?? "");
      const v = asNumber(d[valueKey]);
      if (v === null) continue;
      let bucket = byKey.get(k);
      if (!bucket) {
        bucket = [];
        byKey.set(k, bucket);
        order.push(k);
      }
      bucket.push(v);
    }
    void xKey; // caller orders the data by x within each group
    return order.map((k) => ({ key: k, values: byKey.get(k) ?? [] }));
  }, [data, rowKey, xKey, valueKey]);

  if (groups.length === 0) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        No data to display.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {groups.map((g, idx) => {
        const last = g.values.length ? g.values[g.values.length - 1] : null;
        return (
          <div key={g.key} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{
                width: 128,
                flexShrink: 0,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                fontSize: 12,
                color: "var(--w-ink-muted)",
              }}
              title={g.key}
            >
              {g.key}
            </span>
            <Sparkline values={g.values} colour={getSeriesColor(idx)} />
            {last !== null && (
              <span
                style={{
                  fontFamily: "var(--w-font-mono)",
                  fontSize: 12,
                  color: "var(--w-ink)",
                }}
              >
                {fmt(last)}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
