import { CHART_THEME } from "./theme";
import { wrapLabel } from "./format";

export interface PrecisAxisTickProps {
  x?: number;
  y?: number;
  payload?: { value: string | number; offset?: number };
  formatFn?: (value: number) => string;
  orientation?: "left" | "right" | "top" | "bottom";
  maxLabelWidth?: number;
}

const CHAR_WIDTH = CHART_THEME.axisFontSize * 0.6;

/** Custom axis tick — monospace, muted, with multi-line wrapping for long
 * X-axis labels. Ported from the SPA `PrecisAxisTick`. */
export function PrecisAxisTick({
  x,
  y,
  payload,
  formatFn,
  orientation,
  maxLabelWidth,
}: PrecisAxisTickProps) {
  if (!payload) return null;
  const theme = CHART_THEME;
  const value = payload.value;
  const formatted =
    typeof value === "number" && formatFn ? formatFn(value) : String(value);

  let textAnchor: "start" | "middle" | "end" = "middle";
  let dy = 0;
  let dx = 0;
  if (orientation === "left") {
    textAnchor = "end";
    dx = -4;
  } else if (orientation === "right") {
    textAnchor = "start";
    dx = 4;
  } else {
    dy = 10;
  }

  const isXAxis = orientation === "bottom" || orientation === "top" || !orientation;
  const availableWidth =
    maxLabelWidth ?? (isXAxis && payload.offset ? payload.offset * 2 : 0);

  if (isXAxis && availableWidth > 0) {
    const lines = wrapLabel(formatted, availableWidth, CHAR_WIDTH);
    if (lines.length > 1) {
      return (
        <text
          x={x}
          y={y}
          textAnchor="middle"
          fill={theme.axisStroke}
          fontFamily={theme.axisFontFamily}
          fontSize={theme.axisFontSize}
          dominantBaseline="auto"
        >
          {lines.map((line, i) => (
            <tspan key={i} x={x} dy={i === 0 ? dy : theme.axisFontSize * 1.2}>
              {line}
            </tspan>
          ))}
        </text>
      );
    }
  }

  return (
    <text
      x={x}
      y={y}
      dx={dx}
      dy={dy}
      textAnchor={textAnchor}
      fill={theme.axisStroke}
      fontFamily={theme.axisFontFamily}
      fontSize={theme.axisFontSize}
      dominantBaseline="auto"
    >
      {formatted}
    </text>
  );
}
