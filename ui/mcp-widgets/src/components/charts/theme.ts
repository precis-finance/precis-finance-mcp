/**
 * Self-contained chart theme for the MCP widget.
 *
 * Series colours are the Précis brand chart palette, exposed as host-adaptive
 * `--w-chart-*` CSS variables (light/dark values in `host-tokens.css`) so the
 * series follow the host theme. Recharts accepts `var()` for stroke/fill.
 * Structural tokens (grid, axis, tooltip) map to the widget's `--w-*` vars too.
 */
export const CHART_PALETTE = [
  "var(--w-chart-1)",
  "var(--w-chart-2)",
  "var(--w-chart-3)",
  "var(--w-chart-4)",
  "var(--w-chart-5)",
] as const;

export const CHART_THEME = {
  gridStroke: "var(--w-border)",
  gridOpacity: 0.5,
  axisStroke: "var(--w-ink-muted)",
  axisFontFamily: "var(--w-font-mono)",
  labelFontFamily: "var(--w-font-sans)",
  axisFontSize: 10,
  // Solid light/dark fallback when the host supplies only a theme signal.
  tooltipBg: "var(--w-surface-solid)",
  tooltipRadius: "var(--w-radius)",
  tooltipShadow: "var(--w-tooltip-shadow)",
  animationDuration: 200,
  animationEasing: "ease-in-out" as const,
};

export function getSeriesColor(index: number): string {
  return CHART_PALETTE[index % CHART_PALETTE.length];
}

export const defaultFormatFn = (v: number): string => v.toLocaleString();
