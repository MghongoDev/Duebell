// Dashboard behaviour. It lives in a file, not inline, so the content
// security policy can refuse inline script (issue #49). Server data arrives
// in the JSON block with id "invoice-data" and in data- attributes.

const invoiceRows = JSON.parse(document.getElementById("invoice-data").textContent);
const gridElement = document.getElementById("invoice-grid");

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

const invoiceCell = (params) =>
  `<strong>${escapeHtml(params.data.invoice_number)}</strong><span class="muted">${escapeHtml(params.data.description)}</span>`;
const clientCell = (params) =>
  `<span>${escapeHtml(params.data.client_name)}</span><span class="muted">${escapeHtml(params.data.client_email)}</span>`;
const amountCell = (params) =>
  `<span class="amount">${escapeHtml(params.data.amount_display)} ${escapeHtml(params.data.currency)}</span>`;
const statusCell = (params) =>
  `<span class="status status-${params.value.toLowerCase()}">${escapeHtml(params.value)}</span>`;
const riskCell = (params) => {
  if (!params.value) return '<span class="muted">Not scored</span>';
  const reasons = (params.data.risk_reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const ld = params.data.limited_data
    ? `<li class="limited">Limited data: no history yet</li>`
    : "";
  return `<span class="risk risk-${params.value.toLowerCase()}">${escapeHtml(params.value)}</span><ul class="reasons">${reasons}${ld}</ul>`;
};
const actionsCell = (params) => {
  // PayPal holds this draft but the send failed. Send it again, never create
  // a second PayPal invoice (issue #41).
  const unsent = params.data.status === "DRAFT" && params.data.paypal_invoice_id;
  const send = unsent ? `<button class="small" data-action="send" data-id="${params.data.id}">Send</button> ` : "";
  const pay = params.data.closed ? "" : ` <button class="ghost small" data-action="pay" data-id="${params.data.id}">Simulate payment</button>`;
  return `${send}<button class="ghost small" data-action="refresh" data-id="${params.data.id}">Refresh</button>${pay}`;
};

const gridState = { status: "", risk: "" };
const gridOptions = {
  columnDefs: [
    { headerName: "Invoice", field: "invoice_number", cellRenderer: invoiceCell, flex: 2, minWidth: 190 },
    { headerName: "Client", field: "client_name", cellRenderer: clientCell, flex: 1, minWidth: 160 },
    { headerName: "Amount", field: "amount_cents", cellRenderer: amountCell, width: 125, type: "rightAligned" },
    { headerName: "Due", field: "due_display", width: 125, cellClassRules: { "overdue-cell": (params) => params.data.overdue } },
    { headerName: "Status", field: "status", cellRenderer: statusCell, width: 100 },
    { headerName: "Risk", field: "risk_level", cellRenderer: riskCell, flex: 2, minWidth: 220 },
    { headerName: "", field: "id", cellRenderer: actionsCell, width: 210, sortable: false, filter: false },
  ],
  rowData: invoiceRows,
  defaultColDef: { sortable: true, resizable: true, filter: true },
  rowHeight: 88,
  headerHeight: 42,
  rowClassRules: { "is-overdue": (params) => params.data.overdue },
  isExternalFilterPresent: () => gridState.status !== "" || gridState.risk !== "",
  doesExternalFilterPass: (node) =>
    (!gridState.status || node.data.status === gridState.status) &&
    (!gridState.risk || node.data.risk_level === gridState.risk),
  localeText: { noRowsToShow: "No invoices yet. Create the first one above." },
};

const gridLoading = document.getElementById("grid-loading");

if (window.agGrid && gridElement) {
  const gridApi = agGrid.createGrid(gridElement, gridOptions);
  if (gridLoading) gridLoading.hidden = true;
  document.getElementById("grid-search").addEventListener("input", (event) => {
    gridApi.setGridOption("quickFilterText", event.target.value);
  });
  document.getElementById("grid-status").addEventListener("change", (event) => {
    gridState.status = event.target.value;
    gridApi.onFilterChanged();
  });
  document.getElementById("grid-risk").addEventListener("change", (event) => {
    gridState.risk = event.target.value;
    gridApi.onFilterChanged();
  });
} else if (gridElement) {
  if (gridLoading) gridLoading.hidden = true;
  gridElement.innerHTML = '<div class="empty"><p>Could not load the invoice grid. Refresh the page.</p></div>';
}

function showError(message) {
  const box = document.getElementById("action-error");
  box.textContent = message;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearError() {
  const box = document.getElementById("action-error");
  box.hidden = true;
  box.textContent = "";
}

async function runAction(button, label, path, options = {}) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  clearError();
  try {
    const response = await fetch(path, { ...options, method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      showError(data.error || "That did not work.");
      button.disabled = false;
      button.textContent = original;
      return;
    }
    if (data.warning) window.alert(data.warning);
    window.location.reload();
  } catch (error) {
    showError("Could not reach the server. Check your connection and try again.");
    button.disabled = false;
    button.textContent = original;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const paths = { pay: "simulate-payment", send: "send", refresh: "refresh" };
  const path = paths[button.dataset.action] || "refresh";
  const label = button.dataset.action === "send" ? "Sending..." : "Working...";
  runAction(button, label, `/api/invoices/${button.dataset.id}/${path}`);
});

const scanButton = document.getElementById("scan-reminders");
scanButton.addEventListener("click", () => {
  runAction(scanButton, "Scanning...", "/api/reminders/scan");
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-post]");
  if (!button) return;
  if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
  runAction(button, button.dataset.busy || "Working...", button.dataset.post);
});

const createForm = document.querySelector('input[name="client_email"]').form;
const planOffer = document.getElementById("plan-offer");
let previewTimer = null;
async function previewRisk() {
  const body = new URLSearchParams(new FormData(createForm));
  try {
    const response = await fetch("/api/risk/preview", { method: "POST", body });
    if (!response.ok) return;
    const data = await response.json();
    const high = data.level === "HIGH";
    const chosen = createForm.elements.instalments.value;
    planOffer.hidden = !high && !chosen;
  } catch (error) {
    // The offer is optional; the form still works without it.
  }
}
for (const name of ["client_email", "amount", "currency", "terms_days"]) {
  createForm.elements[name].addEventListener("change", () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(previewRisk, 150);
  });
}

const refitButton = document.getElementById("risk-refit");
refitButton.addEventListener("click", () => {
  runAction(refitButton, "Refitting...", "/api/risk/refit");
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-reminder-action]");
  if (!button) return;
  const action = button.dataset.reminderAction;
  const id = button.dataset.id;
  if (action === "save") {
    const field = document.querySelector(`[data-draft-id="${id}"]`);
    const body = new URLSearchParams({ draft_text: field ? field.value : "" });
    runAction(button, "Saving...", `/api/reminders/${id}/edit`, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    return;
  }
  if (action === "approve") {
    const picker = document.querySelector(`[data-channel-for="${id}"]`);
    const body = new URLSearchParams({ channel: picker ? picker.value : "paypal" });
    runAction(button, "Sending...", `/api/reminders/${id}/approve`, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    return;
  }
  runAction(button, "Working...", `/api/reminders/${id}/${action}`);
});

document.querySelector(".invoice-form").addEventListener("input", (event) => {
  event.target.classList.remove("missing");
  event.target.removeAttribute("aria-invalid");
});

document.getElementById("ai-fill").addEventListener("click", async () => {
  const text = document.getElementById("ai-text").value.trim();
  if (!text) {
    showError("Write a sentence or two first.");
    return;
  }
  const button = document.getElementById("ai-fill");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Filling...";
  clearError();
  try {
    const response = await fetch("/parse", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ description: text }),
    });
    const data = await response.json();
    if (!response.ok) {
      showError(data.error || "Could not parse that. Fill the form instead.");
    } else {
      // A field the AI left out comes back empty and named in `missing`.
      // Mark it so the person fills it in; nothing is guessed (issue #47).
      const missing = new Set(data.missing || []);
      const labels = [];
      for (const [key, value] of Object.entries(data.proposal)) {
        const field = document.querySelector(`.invoice-form [name="${key}"]`);
        if (!field) continue;
        field.value = value;
        if (missing.has(key)) {
          field.setAttribute("aria-invalid", "true");
          field.classList.add("missing");
          labels.push(field.closest("label")?.querySelector("span")?.textContent || key);
        } else {
          field.removeAttribute("aria-invalid");
          field.classList.remove("missing");
          if (field.tagName === "SELECT" && field.value !== String(value)) {
            showError(`${value} is not an allowed currency. Pick one from the list.`);
          }
        }
      }
      if (labels.length) {
        showError(`The AI left these out: ${labels.join(", ")}. Fill them in before you create the invoice.`);
      }
      previewRisk();
    }
  } catch (error) {
    showError("Could not reach the server. Check your connection and try again.");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});
