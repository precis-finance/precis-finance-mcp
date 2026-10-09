/**
 * MCP chart widget. Binds to the `chart` block (structuredContent) emitted by
 * `eval_chart_transform` and dispatches by `chart_type` to Précis-themed
 * Recharts/HTML wrappers — line, bar, area, waterfall, scatter, horizontal_bar,
 * forecast_line, cohort_heatmap, sparkline_row (the full eval_chart_transform
 * set); unknown types fall back to a table. The chart `data` array is raw
 * numbers — `y_format` / `y_scale` / `y_decimals` drive display only.
 */
import type { ReactNode } from "react";
import { useWidgetApp, useSyncDocumentHeight } from "../shared/host";
import { buildFormatY } from "./charts/format";
import { getSeriesColor } from "./charts/theme";
import { PrecisLineChart } from "./charts/PrecisLineChart";
import { PrecisBarChart } from "./charts/PrecisBarChart";
import { PrecisAreaChart } from "./charts/PrecisAreaChart";
import { PrecisWaterfallChart } from "./charts/PrecisWaterfallChart";
import { PrecisScatterChart } from "./charts/PrecisScatterChart";
import { PrecisHorizontalBarChart } from "./charts/PrecisHorizontalBarChart";
import { PrecisForecastLineChart } from "./charts/PrecisForecastLineChart";
import { PrecisCohortHeatmap } from "./charts/PrecisCohortHeatmap";
import { PrecisSparklineRow } from "./charts/PrecisSparklineRow";

interface RefLine {
  axis?: string;
  value?: number | string;
  label?: string;
}

interface ChartCaption {
  period?: string;
  scenarios?: string[];
  scale?: string;
  generated_at?: string;
}

interface ChartBlock {
  chart_type?: string;
  data?: Record<string, unknown>[];
  x_key?: string;
  y_keys?: string[];
  x_format?: string;
  x_scale?: number;
  y_format?: string;
  y_scale?: number;
  y_decimals?: number;
  // Dual Y axis (line / bar / area)
  y_axis_map?: Record<string, "left" | "right">;
  y_format_right?: string;
  y_decimals_right?: number;
  y_label_right?: string;
  // Scatter encodings (scatter only): bubble size + categorical colour
  size_key?: string;
  size_label?: string;
  color_key?: string;
  // forecast_line: x value where forecast begins
  forecast_from?: string | number;
  // cohort_heatmap / sparkline_row: cell/plotted value key
  value_key?: string;
  // forecast_line: confidence band keys
  confidence_band?: { upper_key: string; lower_key: string };
  series_labels?: Record<string, string>;
  series_colours?: Record<string, string>;
  stacked?: boolean;
  title?: string;
  reference_lines?: RefLine[];
  caption?: ChartCaption;
}

const HEIGHT = 320;

function mapRefLines(lines?: RefLine[]) {
  if (!lines) return undefined;
  const y = lines.filter((l) => l.axis === "y" && typeof l.value === "number");
  return y.length ? y.map((l) => ({ y: l.value as number, label: l.label })) : undefined;
}

function captionLine(c?: ChartCaption): string {
  if (!c) return "";
  const parts: string[] = [];
  if (c.scenarios?.length) parts.push(c.scenarios.join(" · "));
  if (c.period) parts.push(c.period);
  if (c.scale) parts.push(c.scale);
  if (c.generated_at) parts.push(`Data as at ${c.generated_at}`);
  return parts.join(" · ");
}

// waterfall / scatter aren't ported yet — show the data as a table so the
// figures still reach the user.
function FallbackTable({
  block,
  formatY,
}: {
  block: ChartBlock;
  formatY: (v: number) => string;
}) {
  const cols = [block.x_key ?? "", ...(block.y_keys ?? [])];
  return (
    <table
      style={{
        width: "100%",
        borderCollapse: "collapse",
        fontFamily: "var(--w-font-sans)",
        fontSize: 13,
      }}
    >
      <thead>
        <tr style={{ borderBottom: "1px solid var(--w-border)" }}>
          {cols.map((c) => (
            <th
              key={c}
              style={{
                padding: "6px 8px",
                textAlign: c === block.x_key ? "left" : "right",
                color: "var(--w-ink-muted)",
                fontWeight: 500,
              }}
            >
              {block.series_labels?.[c] ?? c}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {(block.data ?? []).map((row, i) => (
          <tr key={i} style={{ borderBottom: "1px solid var(--w-border)" }}>
            {cols.map((c) => {
              const v = row[c];
              const isX = c === block.x_key;
              return (
                <td
                  key={c}
                  style={{
                    padding: "4px 8px",
                    textAlign: isX ? "left" : "right",
                    fontFamily: isX ? "var(--w-font-sans)" : "var(--w-font-mono)",
                  }}
                >
                  {isX || typeof v !== "number" ? String(v ?? "") : formatY(v)}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Chart() {
  const { structuredContent, error } = useWidgetApp("precis-chart");
  useSyncDocumentHeight();

  if (error) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        Unable to connect to the host: {error.message}
      </div>
    );
  }

  const block = structuredContent as ChartBlock;
  const data = block.data ?? [];
  const xKey = block.x_key ?? "";
  const yKeys = block.y_keys ?? [];

  if (!data.length || !yKeys.length) {
    return (
      <div style={{ padding: "16px", color: "var(--w-ink-muted)" }}>
        No chart data to display.
      </div>
    );
  }

  const formatY = buildFormatY(block.y_format, block.y_scale, block.y_decimals);
  const formatYRight = block.y_axis_map
    ? buildFormatY(block.y_format_right, block.y_scale, block.y_decimals_right)
    : undefined;
  const formatX = buildFormatY(block.x_format, block.x_scale);
  const seriesLabels = yKeys.map((k) => block.series_labels?.[k] ?? k);
  const seriesColours = yKeys.map((k, i) => block.series_colours?.[k] ?? getSeriesColor(i));
  const refLines = mapRefLines(block.reference_lines);
  const caption = captionLine(block.caption);

  let chart: ReactNode;
  switch (block.chart_type) {
    case "line":
      chart = (
        <PrecisLineChart
          data={data}
          xKey={xKey}
          yKeys={yKeys}
          seriesLabels={seriesLabels}
          seriesColours={seriesColours}
          formatY={formatY}
          yAxisMap={block.y_axis_map}
          formatYRight={formatYRight}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    case "bar":
      chart = (
        <PrecisBarChart
          data={data}
          xKey={xKey}
          yKeys={yKeys}
          seriesLabels={seriesLabels}
          seriesColours={seriesColours}
          formatY={formatY}
          yAxisMap={block.y_axis_map}
          formatYRight={formatYRight}
          stacked={block.stacked}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    case "area":
      chart = (
        <PrecisAreaChart
          data={data}
          xKey={xKey}
          yKeys={yKeys}
          seriesLabels={seriesLabels}
          seriesColours={seriesColours}
          formatY={formatY}
          yAxisMap={block.y_axis_map}
          formatYRight={formatYRight}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    case "waterfall":
      chart = (
        <PrecisWaterfallChart
          data={data}
          xKey={xKey}
          yKeys={yKeys}
          formatY={formatY}
          height={HEIGHT}
        />
      );
      break;
    case "horizontal_bar":
      chart = (
        <PrecisHorizontalBarChart
          data={data}
          xKey={xKey}
          yKey={yKeys[0]}
          seriesLabel={yKeys[0] ? block.series_labels?.[yKeys[0]] : undefined}
          seriesColour={seriesColours[0]}
          formatY={formatY}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    case "forecast_line":
      chart = (
        <PrecisForecastLineChart
          data={data}
          xKey={xKey}
          yKey={yKeys[0]}
          forecastFrom={block.forecast_from ?? ""}
          confidenceBand={
            block.confidence_band
              ? {
                  upperKey: block.confidence_band.upper_key,
                  lowerKey: block.confidence_band.lower_key,
                }
              : undefined
          }
          seriesLabel={yKeys[0] ? block.series_labels?.[yKeys[0]] : undefined}
          seriesColour={seriesColours[0]}
          formatY={formatY}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    case "cohort_heatmap":
      chart = (
        <PrecisCohortHeatmap
          data={data}
          rowKey={yKeys[0]}
          colKey={xKey}
          valueKey={block.value_key ?? yKeys[0]}
          valueFormat={block.y_format}
          decimals={block.y_decimals}
        />
      );
      break;
    case "sparkline_row":
      chart = (
        <PrecisSparklineRow
          data={data}
          rowKey={yKeys[0]}
          xKey={xKey}
          valueKey={block.value_key ?? yKeys[0]}
          valueFormat={block.y_format}
          decimals={block.y_decimals}
        />
      );
      break;
    case "scatter":
      chart = (
        <PrecisScatterChart
          data={data}
          xKey={xKey}
          yKey={yKeys[0]}
          seriesLabel={yKeys[0] ? block.series_labels?.[yKeys[0]] : undefined}
          seriesColour={seriesColours[0]}
          formatX={formatX}
          formatY={formatY}
          sizeKey={block.size_key}
          sizeLabel={block.size_label}
          colourKey={block.color_key}
          referenceLines={refLines}
          height={HEIGHT}
        />
      );
      break;
    default:
      chart = <FallbackTable block={block} formatY={formatY} />;
  }

  const CHART_TYPES = [
    "line", "bar", "area", "waterfall", "scatter",
    "horizontal_bar", "forecast_line", "cohort_heatmap", "sparkline_row",
  ];
  // HTML-based charts (heatmap, sparkline row) size to content, not a fixed
  // chart height — treat them like the table fallback for layout.
  const VARIABLE_HEIGHT = ["cohort_heatmap", "sparkline_row"];
  const isFallback =
    !CHART_TYPES.includes(block.chart_type ?? "") ||
    VARIABLE_HEIGHT.includes(block.chart_type ?? "");

  return (
    <div style={{ padding: "8px" }}>
      {block.title && (
        <div
          style={{ fontWeight: 500, marginBottom: "2px", fontFamily: "var(--w-font-sans)" }}
        >
          {block.title}
        </div>
      )}
      {caption && (
        <div style={{ color: "var(--w-ink-faint)", fontSize: "12px", marginBottom: "8px" }}>
          {caption}
        </div>
      )}
      <div style={isFallback ? undefined : { width: "100%", height: HEIGHT }}>{chart}</div>
    </div>
  );
}
