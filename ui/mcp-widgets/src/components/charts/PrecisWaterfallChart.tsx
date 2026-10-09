/**
 * PrecisWaterfallChart — variance bridge chart (raw SVG + d3-scale). MCP widget
 * port of the SPA component; colour tokens mapped to the host-adaptive --w-*
 * vars. Expects rows with `bar_type` ("total"|"delta") and a value field.
 */
import { useMemo } from "react";
import { scaleBand, scaleLinear } from "d3-scale";
import { useChartDimensions } from "./useChartDimensions";
import { CHART_THEME, defaultFormatFn } from "./theme";
import { wrapLabel } from "./format";

export interface PrecisWaterfallChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  yKeys: string[];
  height?: number;
  formatY?: (value: number) => string;
  seriesLabels?: string[];
  seriesColours?: string[];
  width?: number;
}

interface ComputedBar {
  label: string;
  value: number;
  barType: "total" | "delta";
  y0: number;
  y1: number;
  runningTotal: number;
  colour: string;
}

const BASE_MARGINS = { top: 24, right: 16, bottom: 40, left: 64 };
const BAR_PADDING = 0.4;
const CONNECTOR_DASH = "3 3";
const LABEL_FONT_SIZE = 10;
const LABEL_OFFSET = 6;
const CHAR_WIDTH = CHART_THEME.axisFontSize * 0.6;
const LINE_HEIGHT = CHART_THEME.axisFontSize * 1.2;

export function PrecisWaterfallChart({
  data,
  xKey,
  yKeys,
  height = 320,
  formatY = defaultFormatFn,
  width: widthOverride,
}: PrecisWaterfallChartProps) {
  const theme = CHART_THEME;
  const { containerRef, width: measuredWidth } = useChartDimensions();
  const width = widthOverride && widthOverride > 0 ? widthOverride : measuredWidth;
  const valueKey = yKeys[0] ?? "value";

  const bars = useMemo<ComputedBar[]>(() => {
    if (!data || data.length === 0) return [];
    let runningTotal = 0;
    const result: ComputedBar[] = [];
    for (const row of data) {
      const label = String(row[xKey] ?? "");
      const value = Number(row[valueKey] ?? 0);
      // Canonical flag is `bar_type: "total"`; also honour the common
      // conventions a model reaches for (total/isTotal booleans, Plotly's
      // `measure`) so a reasonable transform still anchors totals to baseline.
      const isTotal =
        row["bar_type"] === "total" ||
        row["total"] === true ||
        row["isTotal"] === true ||
        row["measure"] === "total" ||
        row["measure"] === "absolute";
      const barType: "total" | "delta" = isTotal ? "total" : "delta";
      if (barType === "total") {
        const y0 = Math.min(0, value);
        const y1 = Math.max(0, value);
        runningTotal = value;
        result.push({ label, value, barType, y0, y1, runningTotal, colour: "var(--w-ink-muted)" });
      } else {
        const start = runningTotal;
        runningTotal = start + value;
        const y0 = Math.min(start, runningTotal);
        const y1 = Math.max(start, runningTotal);
        const colour = value >= 0 ? "var(--w-positive)" : "var(--w-negative)";
        result.push({ label, value, barType, y0, y1, runningTotal, colour });
      }
    }
    return result;
  }, [data, xKey, valueKey]);

  const prelimInnerWidth = width - BASE_MARGINS.left - BASE_MARGINS.right;
  const labelSlotWidth = bars.length > 0 ? prelimInnerWidth / bars.length : 100;
  const wrappedLabels = useMemo(
    () => bars.map((b) => wrapLabel(b.label, labelSlotWidth, CHAR_WIDTH)),
    [bars, labelSlotWidth],
  );
  const maxLabelLines = useMemo(
    () => wrappedLabels.reduce((max, lines) => Math.max(max, lines.length), 1),
    [wrappedLabels],
  );
  const bottomMargin = 16 + maxLabelLines * LINE_HEIGHT;
  const margins = { ...BASE_MARGINS, bottom: bottomMargin };

  const innerWidth = width - margins.left - margins.right;
  const innerHeight = height - margins.top - margins.bottom;

  const xScale = useMemo(
    () =>
      scaleBand<string>()
        .domain(bars.map((b) => b.label))
        .range([0, Math.max(0, innerWidth)])
        .padding(BAR_PADDING),
    [bars, innerWidth],
  );

  const yScale = useMemo(() => {
    if (bars.length === 0) return scaleLinear().domain([0, 1]).range([Math.max(0, innerHeight), 0]);
    let minVal = 0;
    let maxVal = 0;
    for (const b of bars) {
      minVal = Math.min(minVal, b.y0);
      maxVal = Math.max(maxVal, b.y1);
    }
    const range = maxVal - minVal || 1;
    const padded = range * 0.1;
    return scaleLinear().domain([minVal - padded, maxVal + padded]).range([Math.max(0, innerHeight), 0]).nice();
  }, [bars, innerHeight]);

  const yTicks = useMemo(() => yScale.ticks(5), [yScale]);

  if (!data || data.length === 0 || width <= 0) {
    return <div ref={containerRef} style={{ width: "100%", height }} />;
  }

  return (
    <div ref={containerRef} style={{ width: "100%" }}>
      <svg width={width} height={height} role="img" aria-label="Waterfall chart">
        <g transform={`translate(${margins.left},${margins.top})`}>
          {yTicks.map((tick) => (
            <line key={`grid-${tick}`} x1={0} x2={innerWidth} y1={yScale(tick)} y2={yScale(tick)}
              stroke={theme.gridStroke} strokeOpacity={theme.gridOpacity} strokeDasharray={CONNECTOR_DASH} />
          ))}
          {yTicks.map((tick) => (
            <text key={`ytick-${tick}`} x={-8} y={yScale(tick)} textAnchor="end" dominantBaseline="middle"
              fontFamily={theme.axisFontFamily} fontSize={theme.axisFontSize} fill={theme.axisStroke}>
              {formatY(tick)}
            </text>
          ))}
          <line x1={0} x2={0} y1={0} y2={innerHeight} stroke={theme.gridStroke} strokeOpacity={theme.gridOpacity} />
          {bars.map((bar, idx) => {
            if (idx === 0 || bar.barType === "total") return null;
            const prev = bars[idx - 1];
            const connY = yScale(prev.runningTotal);
            const prevX = (xScale(prev.label) ?? 0) + xScale.bandwidth();
            const currX = xScale(bar.label) ?? 0;
            return (
              <line key={`conn-${idx}`} x1={prevX} x2={currX} y1={connY} y2={connY}
                stroke={theme.axisStroke} strokeDasharray={CONNECTOR_DASH} strokeOpacity={0.5} />
            );
          })}
          {bars.map((bar) => {
            const x = xScale(bar.label) ?? 0;
            const bw = xScale.bandwidth();
            const yTop = yScale(bar.y1);
            const yBottom = yScale(bar.y0);
            return (
              <rect key={`bar-${bar.label}`} x={x} y={yTop} width={bw} height={Math.max(0, yBottom - yTop)}
                fill={bar.colour} rx={2} />
            );
          })}
          {bars.map((bar) => {
            const x = (xScale(bar.label) ?? 0) + xScale.bandwidth() / 2;
            let y: number;
            let baseline: "auto" | "hanging";
            if (bar.barType === "total" || bar.value >= 0) {
              y = yScale(bar.y1) - LABEL_OFFSET;
              baseline = "auto";
            } else {
              y = yScale(bar.y0) + LABEL_OFFSET;
              baseline = "hanging";
            }
            return (
              <text key={`label-${bar.label}`} x={x} y={y} textAnchor="middle" dominantBaseline={baseline}
                fontFamily={theme.axisFontFamily} fontSize={LABEL_FONT_SIZE} fill={bar.colour}>
                {formatY(bar.value)}
              </text>
            );
          })}
          {bars.map((bar, idx) => {
            const x = (xScale(bar.label) ?? 0) + xScale.bandwidth() / 2;
            const lines = wrappedLabels[idx];
            return (
              <text key={`xlabel-${bar.label}`} x={x} y={innerHeight + 12} textAnchor="middle" dominantBaseline="auto"
                fontFamily={theme.axisFontFamily} fontSize={theme.axisFontSize} fill={theme.axisStroke}>
                {lines.map((line, i) => (
                  <tspan key={i} x={x} dy={i === 0 ? 0 : LINE_HEIGHT}>{line}</tspan>
                ))}
              </text>
            );
          })}
          <line x1={0} x2={innerWidth} y1={innerHeight} y2={innerHeight} stroke={theme.gridStroke} />
        </g>
      </svg>
    </div>
  );
}
