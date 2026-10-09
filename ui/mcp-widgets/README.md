# Précis MCP Widgets

Self-contained HTML bundles served through MCP Apps `resources/read`, linked
from tool descriptors by `_meta.ui.resourceUri`. Each widget reads the render
block from the tool result's `_meta` (with `structuredContent` as a fallback)
and renders it using host-adaptive CSS variables. Précis fonts are not bundled.

## Layout

```
src/
  widgets/<name>.html        — one HTML entry per widget
  entries/<name>.tsx          — React mounting code that reads structuredContent
  components/<Name>.tsx       — the renderer itself
  shared/host.ts              — bridge to host's callServerTool API
  shared/host-tokens.css      — host CSS-variable contract + Précis semantic colour
../../precis_mcp/mcp_external/widgets/<name>.html — packaged build artifact
```

## Build

```bash
cd ui/mcp-widgets
npm install
npm run build
```

`vite-plugin-singlefile` inlines all JS and CSS into the HTML output.  The
build writes into `precis_mcp/mcp_external/widgets/`, which is included in the
Python wheel. The framing registry advertises only available bundles; resource
loading serves their HTML without depending on a repository-relative UI path.

## Adding a widget

1. Add a row to `_WIDGET_URI` in `precis_mcp/mcp_external/framing.py`.
2. Create `src/widgets/<name>.html`, `src/entries/<name>.tsx`, and
   `src/components/<Name>.tsx`.
3. `npm run build` — rebuild the packaged resource, then deploy it with the
   server so the linked widget is available to hosts.

## Host bridge

`shared/host.ts` uses `@modelcontextprotocol/ext-apps` for the MCP Apps handshake,
tool-result delivery and host theme changes. Refresh and drill-through actions
use `callServerTool(name, args)`, which the host routes to the MCP server.

## Host-token CSS

`shared/host-tokens.css` maps Précis-semantic style intents onto host
CSS variables with system-font fallbacks.  The host decides how it looks;
Précis decides what the numbers mean and how they're formatted.

Statement labels and chart tooltip labels use sans-serif text; numerical cells
and tooltip values use monospace. Chart series keep the Précis light/dark
palette. The outer widget surface remains transparent when the host supplies
no background; `--w-surface-solid` provides an opaque light/dark fallback for
tooltips, heatmaps and frozen table labels. Tooltips use host radius/shadow
tokens with quiet fallbacks. Heatmaps mix 8–30% chart teal into that surface and
keep the theme's primary text throughout the scale, avoiding low-contrast white
numbers on light teal. Host tokens always take precedence.
