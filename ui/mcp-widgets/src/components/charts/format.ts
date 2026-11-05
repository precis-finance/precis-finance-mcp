/**
 * Self-contained number formatting + label wrapping for the MCP chart widget.
 * A trimmed port of the SPA's `lib/format.ts` + `charts/wrapLabel.ts` — the
 * Engine chart data is already scaled; chart tools force `y_scale=0` to
 * preserve those units. `y_decimals`/`y_format` control display rounding/symbols.
 */
const EN_DASH = "–";
const EURO = "€";
const LOCALE = "en-IE";

function toNumber(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function formatScale(raw: number, scale: number): number {
  return scale ? raw / 10 ** scale : raw;
}

export function formatValue(
  val: unknown,
  format?: string,
  options?: { scale?: number; decimals?: number },
): string {
  if (val === null || val === undefined) return EN_DASH;
  const raw = toNumber(val);
  if (raw === null) return String(val);

  const scale = options?.scale ?? 0;
  const num = scale ? formatScale(raw, scale) : raw;
  const decimals = options?.decimals ?? 0;
  const fmt = { minimumFractionDigits: decimals, maximumFractionDigits: decimals };

  if (format === "currency") {
    return num < 0
      ? `${EN_DASH}${EURO}${Math.abs(num).toLocaleString(LOCALE, fmt)}`
      : `${EURO}${num.toLocaleString(LOCALE, fmt)}`;
  }
  if (format === "percent") {
    const dp = decimals === 0 && options?.decimals === undefined ? 1 : decimals;
    const abs = Math.abs(num).toFixed(dp);
    return num < 0 ? `${EN_DASH}${abs}%` : `${abs}%`;
  }
  const formatted = Math.abs(num).toLocaleString(LOCALE, {
    maximumFractionDigits: Math.max(decimals, 0),
  });
  return num < 0 ? `${EN_DASH}${formatted}` : formatted;
}

/** Build the Y-axis / tooltip formatter from the chart spec's format fields. */
export function buildFormatY(
  yFormat?: string,
  yScale?: number,
  yDecimals?: number,
): (v: number) => string {
  const scale = yScale ?? 0;
  const decimals = yDecimals ?? 0;
  return (v: number) => {
    if (yFormat === "currency") return formatValue(v, "currency", { scale, decimals });
    if (yFormat === "percent") return formatValue(v, "percent", { decimals: yDecimals ?? 1 });
    return formatValue(v, "number", { scale, decimals });
  };
}

/** Greedy word-wrap for monospace axis labels. */
export function wrapLabel(text: string, maxWidthPx: number, charWidthPx = 6): string[] {
  if (!text) return [""];
  const maxChars = Math.max(1, Math.floor(maxWidthPx / charWidthPx));
  if (text.length <= maxChars) return [text];
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (!current) current = word;
    else if ((current + " " + word).length <= maxChars) current += " " + word;
    else {
      lines.push(current);
      current = word;
    }
  }
  if (current) lines.push(current);
  return lines;
}
