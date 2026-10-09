import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Chart } from "../components/Chart";
import "../shared/host-tokens.css";

// Chart connects to the MCP Apps host and binds to the eval_chart_transform
// `chart` block once the handshake completes; the entry just mounts it.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Chart />
  </StrictMode>,
);
