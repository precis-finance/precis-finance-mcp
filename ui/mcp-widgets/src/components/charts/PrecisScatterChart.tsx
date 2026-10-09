/**
 * PrecisScatterChart — XY scatter/correlation plot (MCP widget port).
 *
 * Recharts implementation matching the SPA component so points are interactive:
 * hovering shows a tooltip with the point's label and x/y values. Optional
 * encodings: bubble size (ZAxis, `sizeKey`) and categorical colour (`colourKey`
 * → one colour per distinct value, with a legend). Inline point labels show
 * only when the set is sparse enough to stay readable; a linear regression
 * trend line and horizontal reference lines are supported. Themed entirely from
 * host-injected `--w-*` variables.
 */

import {
  ResponsiveContainer,
  ScatterChart,
  Scatter,
  CartesianGrid,
  XAxis,
  YAxis,
  ZAxis,
  Tooltip,
  Legend,
  ReferenceLine,
  LabelList,
} from "recharts";
import type { Payload, NameType, ValueType } from "recharts/types/component/DefaultTooltipContent";
import { CHART_THEME, getSeriesColor, defaultFormatFn } from "./theme";
import { PrecisAxisTick } from "./PrecisAxisTick";

export interface PrecisScatterChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  yKey: string;
  height?: number;
  formatX?: (value: number) => string;
  formatY?: (value: number) => string;
  seriesLabel?: string;
  seriesColour?: string;
  xLabel?: string;
  yLabel?: string;
  labelKey?: string;
  /** Numeric key → bubble size (point area across the data range). */
  sizeKey?: string;
  sizeLabel?: string;
  /** Categorical key → one colour per distinct value, with a legend. */
  colourKey?: string;
  trendLine?: boolean;
  referenceLines?: { y: number; label?: string; stroke?: string }[];
}

const CIRCLE_OPACITY = 0.7;
const LABEL_POINT_THRESHOLD = 12;
const SIZE_RANGE: [number, number] = [40, 400];

function linearRegression(
  points: { x: number; y: number }[],
): { slope: number; intercept: number } | null {
  const n = points.length;
  if (n < 2) return null;
  let sumX = 0;
  let sumY = 0;
  for (const p of points) {
    sumX += p.x;
    sumY += p.y;
  }
  const meanX = sumX / n;
  const meanY = sumY / n;
  let num = 0;
  let den = 0;
  for (const p of points) {
    const dx = p.x - meanX;
    num += dx * (p.y - meanY);
    den += dx * dx;
  }
  if (den === 0) return null;
  const slope = num / den;
  return { slope, intercept: meanY - slope * meanX };
}

function asNumber(val: unknown): number | null {
  if (typeof val === "number" && !isNaN(val)) return val;
  if (typeof val === "string") {
    const n = parseFloat(val);
    return isNaN(n) ? null : n;
  }
  return null;
}

function resolveLabelKey(
  data: Record<string, unknown>[],
  explicit?: string,
): string | null {
  if (explicit) {
    if (data.length > 0 && explicit in data[0]) return explicit;
    return null;
  }
  if (data.length === 0) return null;
  if ("name" in data[0]) return "name";
  if ("label" in data[0]) return "label";
  return null;
}

interface ScatterTooltipProps {
  active?: boolean;
  payload?: ReadonlyArray<Payload<ValueType, NameType>>;
  xKey: string;
  yKey: string;
  labelKey: string | null;
  colourKey?: string;
  sizeKey?: string;
  xLabel?: string;
  yLabel?: string;
  seriesLabel?: string;
  sizeLabel?: string;
  formatX: (v: number) => string;
  formatY: (v: number) => string;
}

function ScatterTooltip({
  active,
  payload,
  xKey,
  yKey,
  labelKey,
  colourKey,
  sizeKey,
  xLabel,
  yLabel,
  seriesLabel,
  sizeLabel,
  formatX,
  formatY,
}: ScatterTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const datum = payload[0]?.payload as Record<string, unknown> | undefined;
  if (!datum) return null;

  const theme = CHART_THEME;
  const header = labelKey
    ? String(datum[labelKey] ?? "")
    : colourKey
      ? String(datum[colourKey] ?? "")
      : "";
  const xv = asNumber(datum[xKey]);
  const yv = asNumber(datum[yKey]);

  const rows: { label: string; value: string }[] = [];
  if (xv !== null) rows.push({ label: xLabel || xKey, value: formatX(xv) });
  if (yv !== null) rows.push({ label: seriesLabel ?? yLabel ?? yKey, value: formatY(yv) });
  if (colourKey && labelKey) {
    const cv = datum[colourKey];
    if (cv != null && cv !== "") rows.push({ label: colourKey, value: String(cv) });
  }
  if (sizeKey) {
    const sv = asNumber(datum[sizeKey]);
    if (sv !== null) rows.push({ label: sizeLabel ?? sizeKey, value: sv.toLocaleString() });
  }

  return (
    <div
      style={{
        background: theme.tooltipBg,
        border: "1px solid var(--w-border)",
        borderRadius: theme.tooltipRadius,
        padding: "8px 12px",
        boxShadow: theme.tooltipShadow,
        minWidth: 120,
      }}
    >
      {header && (
        <div
          style={{
            fontFamily: theme.labelFontFamily,
            fontSize: 11,
            fontWeight: 500,
            color: "var(--w-ink)",
            marginBottom: 4,
          }}
        >
          {header}
        </div>
      )}
      {rows.map((r) => (
        <div
          key={r.label}
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "space-between",
            fontFamily: theme.axisFontFamily,
            fontSize: 12,
            color: "var(--w-ink)",
            lineHeight: "18px",
          }}
        >
          <span style={{ fontFamily: theme.labelFontFamily, color: "var(--w-ink-muted)" }}>{r.label}:</span>
          <span style={{ fontWeight: 500 }}>{r.value}</span>
        </div>
      ))}
    </div>
  );
}

export function PrecisScatterChart({
  data,
  xKey,
  yKey,
  height = 300,
  formatX = defaultFormatFn,
  formatY = defaultFormatFn,
  seriesLabel,
  seriesColour,
  xLabel,
  yLabel,
  labelKey: labelKeyProp,
  sizeKey,
  sizeLabel,
  colourKey,
  trendLine = false,
  referenceLines,
}: PrecisScatterChartProps) {
  const theme = CHART_THEME;
  const labelKey = resolveLabelKey(data, labelKeyProp);
  const showLabels = labelKey !== null && data.length <= LABEL_POINT_THRESHOLD;

  const groups: { key: string; rows: Record<string, unknown>[] }[] = [];
  if (colourKey) {
    const byKey = new Map<string, Record<string, unknown>[]>();
    for (const d of data) {
      const k = String(d[colourKey] ?? "—");
      let bucket = byKey.get(k);
      if (!bucket) {
        bucket = [];
        byKey.set(k, bucket);
        groups.push({ key: k, rows: bucket });
      }
      bucket.push(d);
    }
  }

  let trendSegment: [{ x: number; y: number }, { x: number; y: number }] | null = null;
  if (trendLine && data.length >= 2) {
    const points: { x: number; y: number }[] = [];
    for (const d of data) {
      const xv = asNumber(d[xKey]);
      const yv = asNumber(d[yKey]);
      if (xv !== null && yv !== null) points.push({ x: xv, y: yv });
    }
    const reg = linearRegression(points);
    if (reg) {
      const xs = points.map((p) => p.x);
      const xMin = Math.min(...xs);
      const xMax = Math.max(...xs);
      trendSegment = [
        { x: xMin, y: reg.intercept + reg.slope * xMin },
        { x: xMax, y: reg.intercept + reg.slope * xMax },
      ];
    }
  }

  const labelList = showLabels ? (
    <LabelList
      dataKey={labelKey as string}
      position="right"
      offset={6}
      style={{
        fontFamily: theme.axisFontFamily,
        fontSize: 9,
        fill: "var(--w-ink-muted)",
      }}
    />
  ) : null;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ScatterChart margin={{ top: 16, right: 16, bottom: 24, left: 8 }}>
        <CartesianGrid
          stroke={theme.gridStroke}
          strokeOpacity={theme.gridOpacity}
          strokeDasharray="3 3"
        />
        <XAxis
          type="number"
          dataKey={xKey}
          name={xLabel || xKey}
          domain={["dataMin", "dataMax"]}
          tick={<PrecisAxisTick orientation="bottom" formatFn={formatX} />}
          tickLine={false}
          axisLine={{ stroke: theme.gridStroke }}
          height={40}
          label={
            xLabel
              ? {
                  value: xLabel,
                  position: "insideBottomRight",
                  offset: -4,
                  fill: theme.axisStroke,
                  fontFamily: theme.axisFontFamily,
                  fontSize: theme.axisFontSize,
                }
              : undefined
          }
        />
        <YAxis
          type="number"
          name={yLabel || undefined}
          domain={["dataMin", "dataMax"]}
          tick={<PrecisAxisTick orientation="left" formatFn={formatY} />}
          tickLine={false}
          axisLine={false}
          width={56}
          label={
            yLabel
              ? {
                  value: yLabel,
                  position: "insideTopLeft",
                  offset: 0,
                  fill: theme.axisStroke,
                  fontFamily: theme.axisFontFamily,
                  fontSize: theme.axisFontSize,
                }
              : undefined
          }
        />
        {/* Always present: with sizeKey it drives bubble area; without, it
            pins a constant point size. A sometimes-present ZAxis trips
            Recharts' child parsing. */}
        <ZAxis
          type="number"
          dataKey={sizeKey}
          range={sizeKey ? SIZE_RANGE : [60, 60]}
        />
        <Tooltip
          cursor={{ strokeDasharray: "3 3", stroke: theme.axisStroke }}
          content={
            <ScatterTooltip
              xKey={xKey}
              yKey={yKey}
              labelKey={labelKey}
              colourKey={colourKey}
              sizeKey={sizeKey}
              xLabel={xLabel}
              yLabel={yLabel}
              seriesLabel={seriesLabel}
              sizeLabel={sizeLabel}
              formatX={formatX}
              formatY={formatY}
            />
          }
        />
        {colourKey && (
          <Legend
            wrapperStyle={{
              fontFamily: theme.axisFontFamily,
              fontSize: theme.axisFontSize,
            }}
          />
        )}

        {referenceLines?.map((rl, idx) => (
          <ReferenceLine
            key={`ref-${idx}`}
            y={rl.y}
            stroke={rl.stroke ?? theme.axisStroke}
            strokeDasharray="6 3"
            label={
              rl.label
                ? {
                    value: rl.label,
                    position: "insideTopRight",
                    fill: rl.stroke ?? theme.axisStroke,
                    fontFamily: theme.axisFontFamily,
                    fontSize: theme.axisFontSize,
                  }
                : undefined
            }
          />
        ))}

        {trendSegment && (
          <ReferenceLine
            segment={trendSegment}
            stroke="var(--w-ink-muted)"
            strokeDasharray="6 3"
            strokeWidth={1.5}
            ifOverflow="extendDomain"
          />
        )}

        {colourKey ? (
          groups.map((g, idx) => (
            <Scatter
              key={g.key}
              name={g.key}
              data={g.rows}
              dataKey={yKey}
              fill={getSeriesColor(idx)}
              fillOpacity={CIRCLE_OPACITY}
              isAnimationActive={false}
            >
              {labelList}
            </Scatter>
          ))
        ) : (
          <Scatter
            name={seriesLabel ?? yKey}
            data={data}
            dataKey={yKey}
            fill={seriesColour ?? getSeriesColor(0)}
            fillOpacity={CIRCLE_OPACITY}
            isAnimationActive={false}
          >
            {labelList}
          </Scatter>
        )}
      </ScatterChart>
    </ResponsiveContainer>
  );
}
