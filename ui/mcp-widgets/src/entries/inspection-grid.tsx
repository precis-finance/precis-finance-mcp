import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { InspectionGrid } from "../components/InspectionGrid";
import "../shared/host-tokens.css";

// InspectionGrid connects to the MCP Apps host and binds to the inspect_rows
// `inspection_grid` block once the handshake completes; the entry just mounts it.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <InspectionGrid />
  </StrictMode>,
);
