/**
 * Sparkline — tiny inline trend line (no axes), for the sparkline_row chart
 * (MCP widget port). Pure SVG, scales its path to the value range.
 */

import { getSeriesColor } from "./theme";

export interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  colour?: string;
  showEnd?: boolean;
}

export function Sparkline({
  values,
  width = 64,
  height = 20,
  colour,
  showEnd = true,
}: SparklineProps) {
  const stroke = colour ?? getSeriesColor(0);
  const nums = values.filter((v) => typeof v === "number" && !isNaN(v));
  if (nums.length < 2) {
    return <svg width={width} height={height} aria-hidden="true" />;
  }

  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const range = max - min || 1;
  const pad = 2;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;

  const points = nums.map((v, i) => {
    const x = pad + (i / (nums.length - 1)) * innerW;
    const y = pad + (1 - (v - min) / range) * innerH;
    return [x, y] as const;
  });

  const d = points
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const [lastX, lastY] = points[points.length - 1];

  return (
    <svg width={width} height={height} aria-hidden="true">
      <path
        d={d}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {showEnd && <circle cx={lastX} cy={lastY} r={1.8} fill={stroke} />}
    </svg>
  );
}
