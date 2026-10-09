/**
 * MCP Apps host bridge — built on the official `@modelcontextprotocol/ext-apps`
 * View SDK (`io.modelcontextprotocol/ui`). One path for every host: `useApp()`
 * opens a postMessage transport to `window.parent`, performs the `ui/initialize`
 * handshake, and delivers the tool result via `ontoolresult`. Both claude.ai
 * and ChatGPT implement this bridge, so there is no per-host branching here.
 */
import { useCallback, useEffect, useState } from "react";
import { useApp, useHostStyleVariables } from "@modelcontextprotocol/ext-apps/react";

export type HostTheme = "light" | "dark";

// `_meta` key the render variant carries its widget block under. Must match the
// server (`precis_mcp/mcp_external/framing.py::RENDER_BLOCK_META_KEY`). The model
// sees only the raw engine result on `structuredContent`; the widget binds to
// this block. Some hosts (ChatGPT/Cursor bridges) currently strip custom `_meta`
// before forwarding the tool result — the reader below falls back to
// `structuredContent` when the block is absent so the widget degrades instead of
// crashing.
const RENDER_BLOCK_META_KEY = "precis/renderBlock";

const EMPTY: Record<string, unknown> = {};

export interface WidgetApp {
  /** The payload the widget binds to — the render block from the latest
   * `tools/call` result's `_meta`, falling back to `structuredContent`. */
  structuredContent: Record<string, unknown>;
  /** Host theme; mirrored onto `documentElement[data-theme]` for the token CSS. */
  theme: HostTheme;
  /** Handshake completed. */
  isConnected: boolean;
  /** Connection / handshake error, if any. */
  error: Error | null;
  /** Call a server tool from the widget (refresh / drill-through). No-op until connected. */
  callServerTool: (name: string, args: Record<string, unknown>) => Promise<unknown>;
}

/** Connect the widget to its host and expose the bound data, theme, and a server-callback. */
export function useWidgetApp(appName: string): WidgetApp {
  const [structuredContent, setStructuredContent] =
    useState<Record<string, unknown>>(EMPTY);
  const [theme, setTheme] = useState<HostTheme>("light");

  const { app, isConnected, error } = useApp({
    appInfo: { name: appName, version: "0.0.1" },
    capabilities: {},
    onAppCreated: (created) => {
      created.ontoolresult = (params) => {
        // The render variant delivers the widget block on result `_meta`
        // (hidden from the model); fall back to `structuredContent` for hosts
        // that don't forward `_meta`.
        const meta = params._meta as Record<string, unknown> | undefined;
        const block = meta?.[RENDER_BLOCK_META_KEY];
        const sc = block ?? params.structuredContent;
        if (sc && typeof sc === "object") {
          setStructuredContent(sc as Record<string, unknown>);
        }
      };
      created.onhostcontextchanged = (ctx) => {
        if (ctx.theme === "light" || ctx.theme === "dark") setTheme(ctx.theme);
      };
    },
  });

  // Apply any host-injected CSS variables (--color-*, --font-*) to the root so
  // the token CSS picks up the host palette. Fonts are deliberately not applied
  // — the system-font fallback is the MCP styling posture (host owns the page).
  useHostStyleVariables(app, app?.getHostContext());

  // Seed the theme from the initial host context once connected.
  useEffect(() => {
    const t = app?.getHostContext()?.theme;
    if (t === "light" || t === "dark") setTheme(t);
  }, [app]);

  // Drive the token CSS (`:root[data-theme="dark"]`) and the native color-scheme.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  const callServerTool = useCallback(
    async (name: string, args: Record<string, unknown>) => {
      if (!app) return null;
      return app.callServerTool({ name, arguments: args });
    },
    [app],
  );

  return { structuredContent, theme, isConnected, error, callServerTool };
}

/**
 * Report the rendered content height to the host by writing it onto
 * `document.documentElement`. The SDK's `autoResize` already emits the spec
 * `size-changed` notification (honoured by ChatGPT), but claude.ai ignores it
 * and reads `iframe.contentDocument.documentElement` height directly from the
 * DOM (claude-ai-mcp #69) — so without an explicit height the widget container
 * stays collapsed. Observing the root and writing its height covers the
 * claude.ai path and is harmless on ChatGPT.
 */
export function useSyncDocumentHeight(): void {
  useEffect(() => {
    const root = document.getElementById("root") ?? document.body;
    const apply = () => {
      const h = Math.ceil(root.getBoundingClientRect().height);
      if (h > 0) document.documentElement.style.height = `${h}px`;
    };
    const observer = new ResizeObserver(apply);
    observer.observe(root);
    apply();
    return () => observer.disconnect();
  }, []);
}
