import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { resolve } from "path";
import { readdirSync } from "fs";

// `vite-plugin-singlefile` inlines into ONE document and does not support
// multiple inputs per build, so each widget is built in its own invocation
// selected by the `WIDGET` env var (the `build` script loops over the entries).
// With `WIDGET` unset we build every entry — valid only while there is a single
// widget; the loop is the supported multi-widget path.
const widgetsRoot = resolve(__dirname, "src/widgets");
const distDir = resolve(__dirname, "../../precis_mcp/mcp_external/widgets");
const allEntries = Object.fromEntries(
  readdirSync(widgetsRoot)
    .filter((f) => f.endsWith(".html"))
    .map((f) => [f.replace(/\.html$/, ""), resolve(widgetsRoot, f)])
);
const only = process.env.WIDGET;
const input =
  only && allEntries[only] ? { [only]: allEntries[only] } : allEntries;

export default defineConfig({
  // Root at the widgets dir so each HTML entry flattens to dist/<name>.html
  // (the path the server's resources/read expects) rather than mirroring
  // the source tree into dist/src/widgets/.
  root: widgetsRoot,
  plugins: [react(), viteSingleFile()],
  build: {
    outDir: distDir,
    // Never wipe between per-widget builds: each invocation writes one
    // packaged HTML resource.
    emptyOutDir: false,
    cssCodeSplit: false,
    assetsInlineLimit: Number.MAX_SAFE_INTEGER,
    rollupOptions: {
      input,
    },
  },
});
