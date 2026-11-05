/*
 * Précis task pane. Connection, Members and Convert tabs share session state.
 *   - disconnected → Sign in. Both the /mcp URL (derived from the serving origin)
 *     and the OIDC config (endpoints + client_id, auto-discovered from the /mcp
 *     host) are server-supplied — no URL field, no issuer/client field, no manual
 *     token. Sign-in is the only auth path.
 *   - connected → status + Refresh + Disconnect; formatting lives on the ribbon.
 * The token + discovered config are session-only. On load it also force-refreshes
 * the custom-functions metadata cache so new PRECIS.* functions register without
 * a remove + re-upload.
 */

/* global console, document, Excel, HTMLElement, HTMLSelectElement, Office */

import { getMcpUrl, getUserLabel, isConnected, disconnect, onAuthChange } from "../config";
import { signIn } from "../oauth";
import { fetchDimensions, insertDropdown, DropdownMode } from "./dropdowns";
import { activateSelectedExtract, activationSupported } from "../regions/activate";

let dimensionsLoaded = false;
let conversionRunning = false;
const tabs = ["connection", "members", "convert"] as const;
type Tab = typeof tabs[number];

function selectTab(selected: Tab, focus = false): void {
  for (const tab of tabs) {
    const button = el(`tab-${tab}`);
    button.setAttribute("aria-selected", String(tab === selected));
    button.tabIndex = tab === selected ? 0 : -1;
    el(`panel-${tab}`).hidden = tab !== selected;
  }
  if (focus) el(`tab-${selected}`).focus();
}

Office.onReady(() => {
  el("sideload-msg").style.display = "none";
  el("app-body").style.display = "block";

  // Shared-runtime CF metadata-cache force-refresh (dev convenience).
  try {
    Office.context.document.settings.set("Office.ForceRefreshCustomFunctionsCache", true);
    Office.context.document.settings.saveAsync();
  } catch (e) {
    console.error("CF cache force-refresh failed", e);
  }

  el("signin").onclick = onSignIn;
  el("disconnect").onclick = onDisconnect;
  el("refresh").onclick = onRefresh;
  el("insert-dropdown").onclick = onInsertDropdown;
  el("convert-live").onclick = onConvertLive;
  tabs.forEach((tab, index) => {
    const button = el(`tab-${tab}`);
    button.onclick = () => selectTab(tab);
    button.onkeydown = (event: KeyboardEvent) => {
      let next: number;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next], true);
    };
  });
  for (const id of ["members-connect", "convert-connect"]) {
    el(id).onclick = () => { selectTab("connection"); el("signin").focus(); };
  }
  selectTab("connection");

  // A 401-forced disconnect inside a custom function flips the pane back to
  // the disconnected state (instead of staying falsely "Connected").
  onAuthChange(() => {
    const connected = isConnected();
    render(connected);
    if (!connected) {
      setStatus("Session expired — sign in again.");
    }
  });

  render(isConnected());
});

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`missing element #${id}`);
  }
  return node;
}

function setStatus(msg: string, target: "connection" | "members" = "connection"): void {
  el(`${target}-feedback`).textContent = msg;
}

function setConversionStatus(message: string, state = "progress"): void {
  const feedback = el("conversion-feedback");
  feedback.textContent = message;
  feedback.dataset.state = state;
  feedback.setAttribute("role", state === "error" ? "alert" : "status");
  feedback.scrollIntoView({ block: "nearest" });
}

function renderConversionAvailability(): void {
  const supported = activationSupported();
  (el("convert-live") as HTMLButtonElement).disabled = !supported || !isConnected() || conversionRunning;
  el("conversion-support").textContent = supported
    ? "Other Précis regions are not converted."
    : "Conversion is unavailable in this Excel version. Use Excel on the web or an updated Excel version supporting ExcelApi 1.13 or later.";
}

/** Toggle the connected / disconnected sections. */
function render(connected: boolean): void {
  el("disconnected").style.display = connected ? "none" : "block";
  el("connected").style.display = connected ? "block" : "none";
  el("connection-state").textContent = connected ? "Connected" : "Not connected";
  el("connection-bar").dataset.connected = String(connected);
  for (const tab of ["members", "convert"]) {
    el(`${tab}-signin`).hidden = connected;
    el(`${tab}-controls`).hidden = !connected;
  }
  renderActions();
  if (connected) {
    const user = getUserLabel();
    el("connected-user").textContent = user ? `Signed in as ${user}` : "";
    el("connected-url").textContent = getMcpUrl() ?? "";
    void populateDimensionPicker();
  } else {
    dimensionsLoaded = false;
  }
}

function renderActions(): void {
  for (const id of ["refresh", "disconnect", "insert-dropdown", "dropdown-dim", "dropdown-mode"]) {
    (el(id) as HTMLButtonElement).disabled = !isConnected() || conversionRunning;
  }
  renderConversionAvailability();
}

/** Fill the drop-down dimension picker from `list_dimensions` (once per session). */
async function populateDimensionPicker(): Promise<void> {
  if (dimensionsLoaded) {
    return;
  }
  const picker = el("dropdown-dim") as HTMLSelectElement;
  try {
    const dims = await fetchDimensions();
    picker.innerHTML = "";
    for (const d of dims) {
      const opt = document.createElement("option");
      opt.value = d.key;
      opt.textContent =
        d.kind === "ragged" ? `${d.label} (${d.key}) — hierarchy` : `${d.label} (${d.key})`;
      picker.appendChild(opt);
    }
    dimensionsLoaded = dims.length > 0;
  } catch (e) {
    setStatus(`Could not load dimensions: ${(e as Error).message}`, "members");
  }
}

async function onInsertDropdown(): Promise<void> {
  const picker = el("dropdown-dim") as HTMLSelectElement;
  const dim = picker.value;
  if (!dim) {
    setStatus("Pick a dimension first.", "members");
    return;
  }
  const mode = (el("dropdown-mode") as HTMLSelectElement).value as DropdownMode;
  setStatus(`Inserting ${dim} drop-down…`, "members");
  try {
    const res = await insertDropdown(dim, mode);
    let msg = `Added ${dim} drop-downs to the selected cells. Member lists refresh with workbook data.`;
    if (res.companion === "occupied") {
      msg += " Companion column not written — the adjacent cells aren't empty.";
    } else if (res.companion === "skipped-multi-column") {
      msg += " Companion column skipped for a multi-column selection.";
    } else {
      msg += " The matching name or code was added in the column to the right.";
    }
    setStatus(msg, "members");
  } catch (e) {
    setStatus(`Insert failed: ${(e as Error).message}`, "members");
  }
}

async function onSignIn(): Promise<void> {
  const ok = await signIn(setStatus);
  if (ok) {
    render(true);
    setStatus("Signed in. Workbook tools are ready.");
  }
}

function onDisconnect(): void {
  disconnect();
  render(false);
  setStatus("Disconnected.");
}

async function onRefresh(): Promise<void> {
  setStatus("Refreshing Précis cells…");
  try {
    await Excel.run(async (context) => {
      // Full rebuild re-invokes the non-volatile PRECIS.* functions so they
      // re-fetch from /mcp (the Office.js equivalent of Ctrl+Alt+Shift+F9).
      context.workbook.application.calculate(Excel.CalculationType.fullRebuild);
      await context.sync();
    });
    setStatus("Refreshed — Précis cells re-fetched.");
  } catch (e) {
    setStatus(`Refresh failed: ${(e as Error).message}`);
  }
}

async function onConvertLive(): Promise<void> {
  const button = el("convert-live") as HTMLButtonElement;
  const label = button.textContent;
  conversionRunning = true;
  renderActions();
  button.textContent = "Converting…";
  button.setAttribute("aria-busy", "true");
  try {
    setConversionStatus("Checking the selected export…");
    setConversionStatus(await activateSelectedExtract(setConversionStatus), "success");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setConversionStatus(`Conversion stopped: ${message}`, "error");
  } finally {
    conversionRunning = false;
    button.textContent = label;
    button.setAttribute("aria-busy", "false");
    renderActions();
  }
}
