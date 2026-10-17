// Shared by every sample page: DOM helpers, API key, API calls, header with page links and notifications.

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
};
const fmt = (v, digits = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(digits));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

// ------------------------------------------------------------------ API key

function apiKey() {
  try { return localStorage.getItem("svApiKey") || ""; } catch { return ""; }
}

function askKey() {
  return new Promise((resolve) => {
    let dlg = $("#key-dialog");
    if (!dlg) {
      dlg = el("dialog", { id: "key-dialog" }, el("form", { method: "dialog" },
        el("h2", {}, "App API key"),
        el("p", { class: "muted" }, "One of the APP_API_KEYS values from the server's .env. Stored in this browser only."),
        el("input", { id: "key-input", type: "password", autocomplete: "off", required: true }),
        el("menu", {}, el("button", { value: "ok" }, "Save"))));
      document.body.append(dlg);
    }
    $("#key-input").value = apiKey();
    dlg.onclose = () => {
      try { localStorage.setItem("svApiKey", $("#key-input").value.trim()); } catch {}
      resolve();
    };
    dlg.showModal();
  });
}

// ------------------------------------------------------------------ API calls

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-API-Key": apiKey(), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    await askKey();
    return api(path, { method, body });
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatError(res.status, data.detail));
  return data;
}

function formatError(status, detail) {
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: ["body", "actions", 1, "maxValue"], msg}]
    // Positions are shown 1-based to match the numbering in the editors.
    const where = (loc) => loc.filter((p) => p !== "body").map((p) => (typeof p === "number" ? p + 1 : p)).join(" → ");
    return detail.map((d) => `${where(d.loc)}: ${d.msg.replace(/^Value error, /, "")}`).join("\n");
  }
  return `${status}: ${detail || "request failed"}`;
}

// ------------------------------------------------------------------ header

async function refreshBell() {
  try {
    const { unread } = await api("/v1/notifications?limit=1");
    $("#bell").textContent = unread ? `🔔 ${unread}` : "🔔";
  } catch {}
}

function initHeader(page) {
  const link = (href, label) => el("a", { href, class: page === href ? "active" : "" }, label);
  $("header").replaceChildren(
    el("strong", {}, "StocksVest"),
    el("nav", {}, link("index.html", "Filters"), link("automations.html", "Automations")),
    el("span", { id: "market-status", class: "muted" }),
    el("span", { class: "spacer" }),
    el("a", { id: "bell", href: "automations.html#inbox", title: "Notifications" }, "🔔"),
    el("button", { class: "link", onclick: async () => { await askKey(); location.reload(); } }, "API key"),
  );
  refreshBell();
  setInterval(refreshBell, 60000);
}

async function loadMarketStatus() {
  try {
    const status = await api("/v1/market/status");
    const synced = status.lastIncrementalAt ? new Date(status.lastIncrementalAt).toLocaleTimeString() : "never";
    $("#market-status").textContent = `${status.symbols} halal symbols · last sync ${synced} · ~${status.dataDelayMinutes} min delay`;
  } catch (e) {
    $("#market-status").textContent = `market API: ${e.message}`;
  }
}

async function start(page, main) {
  initHeader(page);
  if (!apiKey()) await askKey();
  await Promise.all([main(), loadMarketStatus()]);
}
