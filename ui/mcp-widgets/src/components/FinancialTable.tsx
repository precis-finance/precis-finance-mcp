/**
 * MCP-portable financial table.  Lightweight read-only view of the engine's
 * tabular result.  Deliberately *not* the full SPA component — the
 * mcp-widget bundle stays self-contained (no @/lib/format, no shadcn, no
 * Tailwind runtime).  Précis-semantic formatting (en-dash negatives,
 * right-aligned figures) lives in this file so the widget is identifiable as
 * a finance artefact in any host.
 *
 * Consumes the `financial_table` block shape emitted by the server
 * (`streaming/blocks/table_builder.build_financial_table_block`): rows carry
 * `row_type`, a top-level `label`, a nested `values` map keyed by column key,
 * `indent`, and a `format`. The payload arrives via the MCP Apps bridge
 * (`useWidgetApp`) after the `ui/initialize` handshake completes.
 */
import { useWidgetApp, useSyncDocumentHeight } from "../shared/host";

type RowType =
  | "header"
  | "line_item"
  | "subtotal"
  | "total"
  | "ratio"
  | "separator";

interface ColumnSpec {
  key: string;
  label: string;
  align?: "left" | "right";
  format?: "number" | "percent" | "currency" | "text";
  decimals?: number;
  variance?: boolean;
  // Metrics-as-columns layout: `group` is the metric label spanning the top
  // header row; `variance_effect` moves to the column since the metric (not the
  // row) defines favourable direction.
  group?: string;
  variance_effect?: "natural" | "inverse" | "neutral";
  // Grand-total column (statement crosstab) — shaded like a total row.
  is_total?: boolean;
}

interface RowSpec {
  row_type?: RowType;
  label?: string;
  values?: Record<string, unknown>;
  indent?: number;
  format?: "number" | "percent" | "currency" | "text";
  decimals?: number;
  variance_effect?: "natural" | "inverse" | "neutral";
}

interface TableCaption {
  description?: string;
  scenarios?: string[];
  period?: string;
  filters?: Record<string, unknown>;
  dimensions?: string[];
  scale?: string;
  generated_at?: string;
}

interface TablePayload {
  rows?: RowSpec[];
  columns?: ColumnSpec[];
  title?: string;
  scale_label?: string;
  caption?: TableCaption;
}

// Build the caption line from the block's caption — mirrors the SPA's
// ChartCaptionBar (scenarios · period · filters · dimensions · scale ·
// "Data as at …") so the MCP table reads identically. The data is already in
// the block; this just surfaces the fields the widget previously dropped.
function captionParts(table: TablePayload): string[] {
  const cap = table.caption ?? {};
  const parts: string[] = [];
  if (cap.scenarios?.length) parts.push(cap.scenarios.join(" · "));
  if (cap.period) parts.push(cap.period);
  if (cap.filters) {
    const f = Object.entries(cap.filters)
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
    if (f) parts.push(f);
  }
  if (cap.dimensions?.length) parts.push(cap.dimensions.join(", "));
  const scale = cap.scale ?? table.scale_label;
  if (scale) parts.push(scale);
  if (cap.generated_at) parts.push(`Data as at ${cap.generated_at}`);
  return parts;
}

const EN_DASH = "–";

// Format a numeric cell. Percent values arrive already as percentages
// (e.g. -35 means -35%), so we append "%" without scaling. Engine values are
// full-precision; `decimals` (resolved per metric/column server-side) controls
// the displayed places. Falls back to a 0–2 range when decimals is absent.
function formatCell(
  value: unknown,
  format?: RowSpec["format"],
  decimals?: number,
): string {
  if (value === null || value === undefined || value === "") return "";
  if (format === "text") return String(value);
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  const neg = n < 0;
  const abs = Math.abs(n);
  const dp = decimals ?? (format === "percent" ? 1 : undefined);
  const body =
    format === "percent"
      ? abs.toFixed(dp ?? 1) + "%"
      : abs.toLocaleString(undefined, {
          minimumFractionDigits: dp ?? 0,
          maximumFractionDigits: dp ?? 2,
        });
  return neg ? `${EN_DASH}${body}` : body;
}

// Background + font per row type. Borders are applied per cell via `rowBorder`
// because the table uses `border-collapse: separate` (required for sticky
// columns to work) — under which `<tr>` borders are not painted.
function rowStyle(type?: RowType): React.CSSProperties {
  switch (type) {
    case "header":
      return { fontWeight: 500, background: "var(--w-surface-muted)" };
    case "subtotal":
      return { fontWeight: 500 };
    case "total":
      return { fontWeight: 600 };
    case "ratio":
      return { color: "var(--w-ink-muted)", fontStyle: "italic" };
    default:
      return {};
  }
}

// Cell-level top/bottom rules for total/subtotal/section rows (see rowStyle).
function rowBorder(type?: RowType): React.CSSProperties {
  switch (type) {
    case "header":
    case "subtotal":
      return { borderTop: "1px solid var(--w-border)" };
    case "total":
      return {
        borderTop: "1px solid var(--w-border-strong)",
        borderBottom: "1px solid var(--w-border-strong)",
      };
    default:
      return {};
  }
}

function varianceCellColor(
  value: unknown,
  column: ColumnSpec,
  row: RowSpec,
): string | undefined {
  // In the metrics-as-columns layout the favourable direction comes from the
  // column (the metric); otherwise from the row.
  const effect = column.variance_effect ?? row.variance_effect;
  if (!column.variance || effect === "neutral") return undefined;
  if (value === null || value === undefined || value === "") return "var(--w-ink-faint)";

  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n) || n === 0) return undefined;

  const inverse = effect === "inverse";
  const favourable = inverse ? n < 0 : n > 0;
  return favourable ? "var(--w-positive)" : "var(--w-negative)";
}

// Group consecutive columns sharing the same `group` into top-header spans.
// Returns null when no column declares a group (→ render a single header row).
function groupSpans(
  columns: ColumnSpec[],
): { label: string; span: number; grouped: boolean }[] | null {
  if (!columns.some((c) => c.group)) return null;
  const spans: { label: string; span: number; grouped: boolean }[] = [];
  for (const c of columns) {
    const label = c.group ?? "";
    const last = spans[spans.length - 1];
    if (last && last.label === label && last.grouped === !!c.group) {
      last.span += 1;
    } else {
      spans.push({ label, span: 1, grouped: !!c.group });
    }
  }
  return spans;
}

export function FinancialTable() {
  const { structuredContent, error } = useWidgetApp("precis-financial-table");
  useSyncDocumentHeight();

  const table = structuredContent as TablePayload;
  const columns = table.columns ?? [];
  const rows = table.rows ?? [];
  const caption = captionParts(table);
  // The first column carries the row label; the rest read from `row.values`.
  const labelKey = columns[0]?.key ?? "label";
  // When metrics-as-columns supplies `group`, render a two-level header
  // (metric spanning its scenarios); otherwise a single header row.
  const valueCols = columns.slice(1);
  const spans = groupSpans(valueCols);
  const thStyle = (align: "left" | "right"): React.CSSProperties => ({
    padding: "6px 8px",
    textAlign: align,
    color: "var(--w-ink-muted)",
    fontWeight: 500,
  });
  // Freeze the label column on horizontal scroll (host owns vertical scroll, so
  // no header freeze / bounded height here). Opaque bg + a divider so scrolled
  // cells don't show through.
  const stickyFirst: React.CSSProperties = {
    position: "sticky",
    left: 0,
    zIndex: 1,
    background: "var(--w-surface-solid)",
    borderRight: "1px solid var(--w-border)",
  };
  // Columns size to content (dynamic — adapts to £ / £'m / % automatically), but
  // bounded: a min so figures stay readable, a max so an outlier label (e.g. a
  // very long cost-centre name) wraps/clips instead of stretching the column.
  const VALUE_COL_MIN = 64, VALUE_COL_MAX = 132, LABEL_COL_MAX = 320;
  const valueColStyle: React.CSSProperties = { minWidth: VALUE_COL_MIN, maxWidth: VALUE_COL_MAX };
  // Value-column headers wrap (readable) rather than truncate.
  const headerWrap: React.CSSProperties = { whiteSpace: "normal", overflowWrap: "anywhere" };
  // Label column: no-wrap so P&L lines read on one line; capped + ellipsis so a
  // pathologically long dimension name can't blow out the layout (full text on hover).
  const labelClip: React.CSSProperties = {
    maxWidth: LABEL_COL_MAX,
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  };
  // Total-column tint: a light band derived from the ink colour so it adapts to
  // light/dark without imposing a page background on the host.
  const totalColBg = "color-mix(in srgb, var(--w-ink) 5%, transparent)";

  if (error) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        Unable to connect to the host: {error.message}
      </div>
    );
  }

  if (!rows.length) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        No rows to display.
      </div>
    );
  }

  return (
    <div style={{ padding: "8px" }}>
      {table.title && (
        <div
          style={{
            fontWeight: 500,
            marginBottom: "2px",
            fontFamily: "var(--w-font-sans)",
          }}
        >
          {table.title}
        </div>
      )}
      {caption.length > 0 && (
        <div
          style={{
            color: "var(--w-ink-faint)",
            fontSize: "12px",
            marginBottom: "8px",
          }}
        >
          {caption.join(" · ")}
        </div>
      )}
      <div style={{ overflowX: "auto" }}>
      <table
        style={{
          width: "max-content",
          minWidth: "100%",
          // `separate` (not `collapse`) so position:sticky works on cells in
          // Chromium; border-spacing 0 keeps the collapsed look.
          borderCollapse: "separate",
          borderSpacing: 0,
          fontFamily: "var(--w-font-sans)",
        }}
      >
        <thead>
          {spans ? (
            <>
              <tr>
                <th
                  rowSpan={2}
                  title={columns[0]?.label}
                  style={{ ...thStyle(columns[0]?.align ?? "left"), ...stickyFirst, zIndex: 2, ...labelClip }}
                >
                  {columns[0]?.label ?? ""}
                </th>
                {spans.map((s, si) => (
                  <th
                    key={si}
                    colSpan={s.span}
                    title={s.grouped ? s.label : undefined}
                    style={{
                      ...thStyle("right"),
                      textAlign: "center",
                      borderBottom: s.grouped ? "1px solid var(--w-border)" : undefined,
                      ...headerWrap,
                    }}
                  >
                    {s.grouped ? s.label : ""}
                  </th>
                ))}
              </tr>
              <tr>
                {valueCols.map((c) => (
                  <th
                    key={c.key}
                    title={c.label}
                    style={{
                      ...thStyle(c.align ?? "right"),
                      ...valueColStyle,
                      ...headerWrap,
                      borderBottom: "1px solid var(--w-border)",
                      ...(c.is_total ? { background: totalColBg } : {}),
                    }}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </>
          ) : (
            <tr>
              {columns.map((c) => {
                const isLabel = c.key === labelKey;
                return (
                  <th
                    key={c.key}
                    title={c.label}
                    style={{
                      ...thStyle(c.align ?? (isLabel ? "left" : "right")),
                      borderBottom: "1px solid var(--w-border)",
                      ...(isLabel
                        ? { ...stickyFirst, zIndex: 2, ...labelClip }
                        : { ...valueColStyle, ...headerWrap, ...(c.is_total ? { background: totalColBg } : {}) }),
                    }}
                  >
                    {c.label}
                  </th>
                );
              })}
            </tr>
          )}
        </thead>
        <tbody>
          {rows.map((row, i) => {
            if (row.row_type === "separator") {
              return (
                <tr key={i}>
                  <td
                    colSpan={columns.length}
                    style={{ height: "6px", borderBottom: "1px solid var(--w-border)" }}
                  />
                </tr>
              );
            }
            return (
              <tr key={i} style={rowStyle(row.row_type)}>
                {columns.map((c) => {
                  const isLabel = c.key === labelKey;
                  if (isLabel) {
                    return (
                      <td
                        key={c.key}
                        title={row.label ?? ""}
                        style={{
                          padding: "4px 8px",
                          paddingLeft: `${8 + (row.indent ?? 0) * 16}px`,
                          textAlign: "left",
                          fontFamily: "var(--w-font-sans)",
                          ...labelClip,
                          ...rowBorder(row.row_type),
                          ...stickyFirst,
                          // Match the row so the frozen label cell is opaque:
                          // header rows carry a muted fill, others the base surface.
                          background: row.row_type === "header" ? "var(--w-surface-muted)" : "var(--w-surface-solid)",
                        }}
                      >
                        {row.label ?? ""}
                      </td>
                    );
                  }
                  return (
                    <td
                      key={c.key}
                      style={{
                        padding: "4px 8px",
                        textAlign: c.align ?? "right",
                        fontFamily: "var(--w-font-mono)",
                        fontVariantNumeric: "tabular-nums",
                        whiteSpace: "nowrap",
                        ...valueColStyle,
                        ...rowBorder(row.row_type),
                        color: varianceCellColor(row.values?.[c.key], c, row),
                        // Grand-total column: shade so it reads as the total band.
                        background: c.is_total ? totalColBg : undefined,
                      }}
                    >
                      {formatCell(
                        row.values?.[c.key],
                        c.format ?? row.format,
                        c.decimals ?? row.decimals,
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
    </div>
  );
}
