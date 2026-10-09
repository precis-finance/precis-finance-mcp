/**
 * PrecisAreaChart — filled area chart (MCP widget port of the SPA component).
 * Filled areas at 15% opacity with solid strokes; optional dual Y axis.
 */

import {
  ResponsiveContainer,
  AreaChart,
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

export interface PrecisAreaChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  yKeys: string[];
  seriesLabels?: string[];
  seriesColours?: string[];
  height?: number;
  formatY?: (value: number) => string;
  /** Maps a y-key to its axis ("left" | "right"); unmapped keys → "left". */
  yAxisMap?: Record<string, "left" | "right">;
  /** Formatter for the right Y axis ticks and tooltip values. */
  formatYRight?: (value: number) => string;
  referenceLines?: ReferenceLineConfig[];
}

export function PrecisAreaChart({
  data,
  xKey,
  yKeys,
  seriesLabels,
  seriesColours,
  height = 300,
  formatY = defaultFormatFn,
  yAxisMap,
  formatYRight,
  referenceLines,
}: PrecisAreaChartProps) {
  const theme = CHART_THEME;
  const hasRight = !!yAxisMap && yKeys.some((k) => yAxisMap[k] === "right");
  const formatYRightFn = formatYRight ?? formatY;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
        <defs>
          {yKeys.map((key, idx) => {
            const color = seriesColours?.[idx] ?? getSeriesColor(idx);
            return (
              <linearGradient
                key={`fill-${key}`}
                id={`wAreaFill-${key}`}
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" stopColor={color} stopOpacity={0.15} />
                <stop offset="100%" stopColor={color} stopOpacity={0.03} />
              </linearGradient>
            );
          })}
        </defs>

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
          yAxisId="left"
          tick={<PrecisAxisTick orientation="left" formatFn={formatY} />}
          tickLine={false}
          axisLine={false}
          width={56}
        />
        {hasRight && (
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={<PrecisAxisTick orientation="right" formatFn={formatYRightFn} />}
            tickLine={false}
            axisLine={false}
            width={56}
          />
        )}
        <Tooltip
          content={
            <PrecisTooltip
              formatFn={formatY}
              formatFnRight={formatYRightFn}
              yAxisMap={yAxisMap}
            />
          }
          cursor={{ stroke: theme.axisStroke, strokeDasharray: "3 3" }}
        />

        {yKeys.map((key, idx) => (
          <Area
            yAxisId={yAxisMap?.[key] === "right" ? "right" : "left"}
            key={key}
            type="monotone"
            dataKey={key}
            name={seriesLabels?.[idx] ?? key}
            stroke={seriesColours?.[idx] ?? getSeriesColor(idx)}
            strokeWidth={2}
            fill={`url(#wAreaFill-${key})`}
            fillOpacity={1}
            dot={false}
            activeDot={{ r: 3, strokeWidth: 0 }}
            animationDuration={theme.animationDuration}
            animationEasing={theme.animationEasing}
          />
        ))}

        {referenceLines?.map((rl, idx) => (
          <ReferenceLine
            yAxisId="left"
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
      </AreaChart>
    </ResponsiveContainer>
  );
}
