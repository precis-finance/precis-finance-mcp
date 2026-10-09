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

export interface PrecisBarChartProps {
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
  stacked?: boolean;
  referenceLines?: ReferenceLineConfig[];
}

/** Précis-themed bar chart (MCP widget port of the SPA `PrecisBarChart`). */
export function PrecisBarChart({
  data,
  xKey,
  yKeys,
  seriesLabels,
  seriesColours,
  height = 300,
  formatY = defaultFormatFn,
  yAxisMap,
  formatYRight,
  stacked = false,
  referenceLines,
}: PrecisBarChartProps) {
  const theme = CHART_THEME;
  const hasRight = !!yAxisMap && yKeys.some((k) => yAxisMap[k] === "right");
  const formatYRightFn = formatYRight ?? formatY;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        margin={{ top: 8, right: 12, bottom: 4, left: 4 }}
        barCategoryGap="20%"
        barGap={2}
      >
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
          cursor={{ fill: "var(--w-surface-muted)", opacity: 0.5 }}
        />
        {yKeys.map((key, idx) => (
          <Bar
            key={key}
            yAxisId={yAxisMap?.[key] === "right" ? "right" : "left"}
            dataKey={key}
            name={seriesLabels?.[idx] ?? key}
            fill={seriesColours?.[idx] ?? getSeriesColor(idx)}
            radius={[2, 2, 0, 0]}
            stackId={stacked ? "stack" : undefined}
            animationDuration={theme.animationDuration}
            animationEasing={theme.animationEasing}
          />
        ))}
        {referenceLines?.map((rl, idx) => (
          <ReferenceLine
            key={`ref-${idx}`}
            yAxisId="left"
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
      </BarChart>
    </ResponsiveContainer>
  );
}
