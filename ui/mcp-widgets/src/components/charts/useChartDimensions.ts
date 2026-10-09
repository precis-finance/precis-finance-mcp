/**
 * useChartDimensions — responsive container hook for custom SVG charts
 * (waterfall, scatter). ResizeObserver tracks the container width; height is
 * caller-controlled. Ported verbatim from the SPA.
 */
import { useRef, useState, useEffect } from "react";

export function useChartDimensions() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        if (w > 0) setWidth(Math.floor(w));
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return { containerRef, width };
}
