/**
 * PrecisForecastLineChart — actuals (solid) → forecast (dashed) split at a
 * boundary, with an optional shaded confidence band (MCP widget port).
 */

import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  Area,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
} from "recharts";
import { CHART_THEME, getSeriesColor, defaultFormatFn } from "./theme";
import { PrecisTooltip } from "./PrecisTooltip";
import { PrecisAxisTick } from "./PrecisAxisTick";
import type { ReferenceLineConfig } from "./PrecisLineChart";

export interface PrecisForecastLineChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  yKey: string;
  /** X value at which forecast begins (boundary point belongs to both segments). */
  forecastFrom: string | number;
  confidenceBand?: { upperKey: string; lowerKey: string };
  seriesLabel?: string;
  seriesColour?: string;
  height?: number;
  formatY?: (value: number) => string;
  referenceLines?: ReferenceLineConfig[];
}

const ACTUAL_KEY = "__actual";
const FORECAST_KEY = "__forecast";

function asNumber(val: unknown): number | null {
  if (typeof val === "number" && !isNaN(val)) return val;
  if (typeof val === "string") {
    const n = parseFloat(val);
    return isNaN(n) ? null : n;
  }
  return null;
}

export function PrecisForecastLineChart({
  data,
  xKey,
  yKey,
  forecastFrom,
  confidenceBand,
  seriesLabel,
  seriesColour,
  height = 300,
  formatY = defaultFormatFn,
  referenceLines,
}: PrecisForecastLineChartProps) {
  const theme = CHART_THEME;
  const colour = seriesColour ?? getSeriesColor(0);
  const label = seriesLabel ?? yKey;

  let reachedForecast = false;
  const rows = data.map((d) => {
    const v = asNumber(d[yKey]);
    const isBoundary = d[xKey] === forecastFrom;
    if (isBoundary) reachedForecast = true;
    return {
      ...d,
      [ACTUAL_KEY]: !reachedForecast || isBoundary ? v : null,
      [FORECAST_KEY]: reachedForecast ? v : null,
    };
  });

  const bandGradientId = "wForecastBandFill";

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
        {confidenceBand && (
          <defs>
            <linearGradient id={bandGradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colour} stopOpacity={0.12} />
              <stop offset="100%" stopColor={colour} stopOpacity={0.04} />
            </linearGradient>
          </defs>
        )}
        <CartesianGrid
          stroke={theme.gridStroke}
          strokeOpacity={theme.gridOpacity}
          strokeDasharray="3 3"
          vertical={false}
        />
        <XAxis
          dataKey={xKey}
          tick={<PrecisAxisTick orientation="bottom" />}
          tickLine={false}
          axisLine={{ stroke: theme.gridStroke }}
          height={50}
        />
        <YAxis
          tick={<PrecisAxisTick orientation="left" formatFn={formatY} />}
          tickLine={false}
          axisLine={false}
          width={56}
        />
        <Tooltip
          content={<PrecisTooltip formatFn={formatY} />}
          cursor={{ stroke: theme.axisStroke, strokeDasharray: "3 3" }}
        />

        {confidenceBand && (
          <Area
            dataKey={confidenceBand.upperKey}
            stroke="none"
            fill={`url(#${bandGradientId})`}
            fillOpacity={1}
            isAnimationActive={false}
            activeDot={false}
          />
        )}

        <ReferenceLine
          x={forecastFrom}
          stroke={theme.axisStroke}
          strokeDasharray="2 2"
          label={{
            value: "Forecast",
            position: "insideTopRight",
            fill: theme.axisStroke,
            fontFamily: theme.axisFontFamily,
            fontSize: theme.axisFontSize,
          }}
        />

        <Line
          type="monotone"
          dataKey={ACTUAL_KEY}
          name={label}
          stroke={colour}
          strokeWidth={2}
          dot={false}
          connectNulls={false}
          activeDot={{ r: 3, strokeWidth: 0 }}
          animationDuration={theme.animationDuration}
          animationEasing={theme.animationEasing}
        />
        <Line
          type="monotone"
          dataKey={FORECAST_KEY}
          name={`${label} (forecast)`}
          stroke={colour}
          strokeWidth={2}
          strokeDasharray="5 4"
          dot={false}
          connectNulls={false}
          activeDot={{ r: 3, strokeWidth: 0 }}
          animationDuration={theme.animationDuration}
          animationEasing={theme.animationEasing}
        />

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
                    fill: theme.axisStroke,
                    fontFamily: theme.axisFontFamily,
                    fontSize: theme.axisFontSize,
                  }
                : undefined
            }
          />
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
