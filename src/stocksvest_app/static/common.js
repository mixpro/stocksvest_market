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
// replaceChildren that skips null/false and flattens nested lists (optional parts of a view).
const setChildren = (node, ...children) =>
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
const fmt = (v, digits = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(digits));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const money = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const signed = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${money(v)}`);
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

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
    el("nav", {}, link("index.html", "Filters"), link("automations.html", "Automations"), link("portfolio.html", "Portfolio"), link("activity.html", "Activity")),
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

// ------------------------------------------------------------------ charts

const SERIES_COLORS = ["#2563eb", "#16a34a", "#f59e0b", "#9333ea", "#dc2626"];

// Line chart with one or more series: [{name, data: [{at, value}], scale?: "left"|"right"}]. Needs lightweight-charts.
function lineChart(container, series, { height = 260 } = {}) {
  container.replaceChildren();
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const box = el("div", { style: `height:${height}px` });
  const legend = el("div", { class: "legend" });
  container.append(legend, box);
  const chart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: dark ? "#e6e8eb" : "#1d2330" },
    grid: { vertLines: { visible: false }, horzLines: { color: dark ? "#2b313a" : "#eef0f3" } },
    timeScale: { timeVisible: true },
    leftPriceScale: { visible: series.some((s) => s.scale === "left") },
    rightPriceScale: { visible: true },
  });
  series.forEach((s, i) => {
    const color = SERIES_COLORS[i % SERIES_COLORS.length];
    const line = chart.addLineSeries({ color, lineWidth: 2, priceScaleId: s.scale === "left" ? "left" : "right",
      lastValueVisible: true, priceLineVisible: false });
    // One value per second at most, oldest first (the chart library requires it).
    const seen = new Map();
    for (const p of s.data) if (p.value !== null && p.value !== undefined) seen.set(Math.floor(new Date(p.at).getTime() / 1000), p.value);
    line.setData([...seen.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
    legend.append(el("span", { style: `color:${color}` }, `● ${s.name}`));
  });
  chart.timeScale().fitContent();
  return chart;
}

// Timeframe buttons: [["1W", 7], ["1M", 30], ...] (days, null = all). Calls onPick(days, label).
function rangeButtons(options, current, onPick) {
  return el("span", { class: "chips" }, ...options.map(([label, days]) =>
    el("button", { class: label === current ? "primary" : "", onclick: () => onPick(days, label) }, label)));
}

function sinceDays(days) {
  return days ? new Date(Date.now() - days * 86400e3).toISOString().slice(0, 19) + "Z" : null;
}
