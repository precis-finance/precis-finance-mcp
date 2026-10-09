import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { FinancialTable } from "../components/FinancialTable";
import "../shared/host-tokens.css";

// FinancialTable connects to the MCP Apps host and binds to the tool result
// once the handshake completes, so the entry just mounts it.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FinancialTable />
  </StrictMode>,
);
