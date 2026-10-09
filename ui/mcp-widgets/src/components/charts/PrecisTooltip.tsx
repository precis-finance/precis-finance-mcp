import type {
  Payload,
  NameType,
  ValueType,
} from "recharts/types/component/DefaultTooltipContent";
import { CHART_THEME, defaultFormatFn, getSeriesColor } from "./theme";

export interface PrecisTooltipProps {
  active?: boolean;
  payload?: ReadonlyArray<Payload<ValueType, NameType>>;
  label?: string | number;
  formatFn?: (value: number) => string;
  /** Formatter for right-axis values. */
  formatFnRight?: (value: number) => string;
  /** Maps a dataKey to its axis; entries on "right" use formatFnRight. */
  yAxisMap?: Record<string, "left" | "right">;
}

/** Précis-styled tooltip — host-adaptive surface, sans labels, monospace figures and coloured dot per
 * series. Ported from the SPA `PrecisTooltip`. */
export function PrecisTooltip({
  active,
  payload,
  label,
  formatFn = defaultFormatFn,
  formatFnRight,
  yAxisMap,
}: PrecisTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;

  return (
    <div
      style={{
        background: CHART_THEME.tooltipBg,
        border: "1px solid var(--w-border)",
        borderRadius: CHART_THEME.tooltipRadius,
        padding: "8px 12px",
        boxShadow: CHART_THEME.tooltipShadow,
        minWidth: 120,
      }}
    >
      {label != null && (
        <div
          style={{
            fontFamily: CHART_THEME.axisFontFamily,
            fontSize: 10,
            color: "var(--w-ink-muted)",
            marginBottom: 4,
          }}
        >
          {label}
        </div>
      )}
      {payload.map((entry, idx) => {
        const displayName = entry.name ?? entry.dataKey;
        const isRight =
          typeof entry.dataKey === "string" &&
          yAxisMap?.[entry.dataKey] === "right";
        const fmt = isRight ? formatFnRight ?? formatFn : formatFn;
        const displayValue =
          typeof entry.value === "number"
            ? fmt(entry.value)
            : String(entry.value ?? "");
        const dotColor = (entry.color as string | undefined) ?? getSeriesColor(idx);
        return (
          <div
            key={String(entry.dataKey ?? idx)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontFamily: CHART_THEME.axisFontFamily,
              fontSize: 12,
              color: "var(--w-ink)",
              lineHeight: "18px",
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: dotColor,
                flexShrink: 0,
              }}
            />
            <span style={{ fontFamily: CHART_THEME.labelFontFamily, color: "var(--w-ink-muted)", marginRight: 2 }}>
              {String(displayName)}:
            </span>
            <span style={{ fontWeight: 500 }}>{displayValue}</span>
          </div>
        );
      })}
    </div>
  );
}
