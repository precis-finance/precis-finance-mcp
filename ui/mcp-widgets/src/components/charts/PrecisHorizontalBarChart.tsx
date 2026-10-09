/**
 * PrecisHorizontalBarChart — top-N ranking bars (MCP widget port). Categories
 * down the left, value bars extending right. Single value series.
 */

import {
  ResponsiveContainer,
  BarChart,
  Bar,
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

export interface PrecisHorizontalBarChartProps {
  data: Record<string, unknown>[];
  /** Category key (rendered down the Y axis). */
  xKey: string;
  /** Value key (rendered along the X axis). */
  yKey: string;
  seriesLabel?: string;
  seriesColour?: string;
  height?: number;
  formatY?: (value: number) => string;
  referenceLines?: ReferenceLineConfig[];
}

const CATEGORY_AXIS_WIDTH = 120;

export function PrecisHorizontalBarChart({
  data,
  xKey,
  yKey,
  seriesLabel,
  seriesColour,
  height = 300,
  formatY = defaultFormatFn,
  referenceLines,
}: PrecisHorizontalBarChartProps) {
  const theme = CHART_THEME;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 8, right: 16, bottom: 4, left: 4 }}
        barCategoryGap="20%"
      >
        <CartesianGrid
          stroke={theme.gridStroke}
          strokeOpacity={theme.gridOpacity}
          strokeDasharray="3 3"
          horizontal={false}
        />
        <XAxis
          type="number"
          tick={<PrecisAxisTick orientation="bottom" formatFn={formatY} />}
          tickLine={false}
          axisLine={{ stroke: theme.gridStroke }}
          height={40}
        />
        <YAxis
          type="category"
          dataKey={xKey}
          tick={<PrecisAxisTick orientation="left" />}
          tickLine={false}
          axisLine={false}
          width={CATEGORY_AXIS_WIDTH}
        />
        <Tooltip
          content={<PrecisTooltip formatFn={formatY} />}
          cursor={{ fill: "var(--w-surface-muted)", opacity: 0.5 }}
        />
        <Bar
          dataKey={yKey}
          name={seriesLabel ?? yKey}
          fill={seriesColour ?? getSeriesColor(0)}
          radius={[0, 2, 2, 0]}
          animationDuration={theme.animationDuration}
          animationEasing={theme.animationEasing}
        />
        {referenceLines?.map((rl, idx) => (
          <ReferenceLine
            key={`ref-${idx}`}
            x={rl.y}
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
      </BarChart>
    </ResponsiveContainer>
  );
}
