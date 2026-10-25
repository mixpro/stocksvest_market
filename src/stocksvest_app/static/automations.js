// Automations page: exercises every automation, schedule, run, switch and notification endpoint.

const DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
const ZONES = ["America/New_York", "Asia/Jerusalem", "UTC", Intl.DateTimeFormat().resolvedOptions().timeZone]
  .filter((z, i, all) => all.indexOf(z) === i);
const ANCHORS = ["PRE_MARKET_OPEN", "REGULAR_OPEN", "REGULAR_CLOSE", "AFTER_HOURS_CLOSE"];

const SCHEDULE_DEFAULTS = {
  MARKET: { anchor: "REGULAR_OPEN", offsetMinutes: 30 },
  DAILY: { atTimes: ["10:00"] },
  WEEKLY: { onDays: ["MON"], atTimes: ["10:00"], everyNWeeks: 1 },
  INTERVAL: { every: 60, unit: "MINUTE", onDays: ["MON", "TUE", "WED", "THU", "FRI"], window: { start: "09:30", end: "16:00" } },
  ONCE: { at: null },
};

const st = { automations: [], filters: [], groups: [], portfolios: [], selectedId: null, draft: null };
const RANK_KEYS = ["DAILY_CHANGE_PERCENT", "LAST_PRICE", "DAILY_VOLUME", "POSITION_VALUE", "UNREALIZED_PNL_PERCENT",
  "MOMENTUM_12_1", "MOMENTUM_6_1", "AVERAGE_DOLLAR_VOLUME_20", "VOLATILITY_20", "FROM_52W_HIGH"];
const ORDER_DEFAULT = {
  type: "PLACE_ORDER", side: "BUY", orderType: "MARKET", timeInForce: "DAY", extendedHours: false,
  selection: { mode: "TOP_N", topN: 3, rankBy: "DAILY_CHANGE_PERCENT", descending: true },
  sizings: [{ mode: "CASH_AMOUNT", value: 500 }],
};
const GUARD_DEFAULT = { maxOrdersPerRun: 10, maxPositionValue: null, maxPositionPercent: null, regularHoursOnly: true, skipBuyIfHeld: true, maxDataAgeMinutes: 30, requireLivePrice: false, maxSpreadPercent: null, rebuyCooldownMinutes: null, maxSectorPercent: null, targetVolatilityPercent: null, maxOpenPositions: null, maxCorrelation: null };

// ------------------------------------------------------------------ loading

async function loadAll() {
  const [a, f, g, p] = await Promise.all([api("/v1/automations"), api("/v1/filters"), api("/v1/filter-groups"), api("/v1/portfolios")]);
  st.portfolios = p.portfolios;
  st.automations = a.automations;
  st.filters = f.filters;
  st.groups = g.groups;
  renderSidebar();
  await renderSwitch();
}

async function renderSwitch() {
  const sw = await api("/v1/automations/switch");
  const on = sw.automationsEnabled;
  const sched = sw.scheduler.running
    ? `scheduler ✓ ${sw.scheduler.lastTickAt ? new Date(sw.scheduler.lastTickAt).toLocaleTimeString() : ""}`
    : "scheduler not running";
  setChildren($("#switch"), el("div", { class: `switch ${on ? "on" : "off"}` },
    el("b", {}, on ? "Scheduled runs ON" : "Scheduled runs OFF"),
    el("span", { class: "grow muted" }, sched),
    el("button", { onclick: () => guard(async () => {
      const reason = on ? prompt("Reason for pausing all automations (optional)") : null;
      await api("/v1/automations/switch", { method: "PUT", body: { automationsEnabled: !on, reason } });
      await renderSwitch();
    }) }, on ? "Turn off" : "Turn on")));
}

// "in 2d 3h", "in 12 min", "now"
function fromNow(iso) {
  const min = Math.round((new Date(iso) - Date.now()) / 60000);
  if (min <= 0) return "now";
  if (min < 60) return `in ${min} min`;
  const h = Math.floor(min / 60), d = Math.floor(h / 24);
  return d ? `in ${d}d ${h % 24}h` : `in ${h}h ${min % 60}m`;
}

function nextRunText(a) {
  if (a.status === "PAUSED") return "paused";
  if (!a.nextRunAt) return "no upcoming runs";
  const t = new Date(a.nextRunAt);
  const day = t.toLocaleDateString(undefined, { weekday: "short" });
  return `next ${day} ${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${fromNow(a.nextRunAt)}`;
}

function renderSidebar() {
  setChildren($("#automation-list"), ...st.automations.map((a) =>
    el("li", { class: `stacked ${a.id === st.selectedId ? "active" : ""}`, onclick: () => select(a.id) },
      el("div", { class: "line" }, el("span", {}, a.name), el("span", { class: `badge ${a.status}` }, a.status.toLowerCase())),
      el("div", { class: "sub" }, nextRunText(a)),
      a.lastRun ? el("div", { class: "sub" }, `last ${when(a.lastRun.ranAt)} · `,
        el("span", { class: `badge ${a.lastRun.status}` }, a.lastRun.status.toLowerCase())) : null)));
}

function select(id) {
  st.selectedId = id;
  const existing = st.automations.find((a) => a.id === id);
  st.draft = existing ? structuredClone(existing) : {
    name: "New automation", description: "",
    runTargets: st.filters.length ? [{ type: "FILTER", id: st.filters[0].id }] : [],
    schedules: [{ type: "MARKET", ...structuredClone(SCHEDULE_DEFAULTS.MARKET) }],
    actions: [{ type: "NOTIFY", title: "{name}: {count} matches", message: "", includeMatchedSymbols: true, skipWhenNoMatches: true }],
  };
  renderSidebar();
  renderEditor();
  showResults(el("p", { class: "muted empty" }, "Preview, run, or open history to see results here."));
}

// ------------------------------------------------------------------ editor

function setError(message) {
  $("#editor .error")?.remove();
  if (message) $("#editor").prepend(el("div", { class: "error" }, message));
}

async function guard(fn) {
  setError(null);
  try { await fn(); } catch (e) { setError(e.message); }
}

const rerender = () => renderEditor();

function textInput(obj, key, attrs = {}) {
  return el("input", { type: "text", value: obj[key] ?? "", ...attrs, oninput: (e) => (obj[key] = e.target.value) });
}

function numInput(obj, key, attrs = {}) {
  return el("input", { type: "number", value: obj[key] ?? "", ...attrs,
    oninput: (e) => (obj[key] = e.target.value === "" ? null : Number(e.target.value)) });
}

function select_(options, current, onchange, attrs = {}) {
  return el("select", { ...attrs, onchange: (e) => onchange(e.target.value) },
    ...options.map((o) => {
      const [value, label] = Array.isArray(o) ? o : [o, o];
      return el("option", { value, selected: value === current }, label);
    }));
}

function dayChips(obj, key) {
  const set = new Set(obj[key] || []);
  return el("span", { class: "chips" }, ...DAYS.map((d) => el("label", {},
    el("input", { type: "checkbox", checked: set.has(d), onchange: (e) => {
      e.target.checked ? set.add(d) : set.delete(d);
      obj[key] = DAYS.filter((x) => set.has(x));
    } }), d.slice(0, 2))));
}

function timesInput(obj) {
  // atTimes as "09:45, 16:30"
  const show = (obj.atTimes || []).map((t) => t.slice(0, 5)).join(", ");
  return el("input", { type: "text", value: show, placeholder: "10:00, 15:30", size: 14,
    oninput: (e) => (obj.atTimes = e.target.value.split(",").map((t) => t.trim()).filter(Boolean)) });
}

function scheduleRow(s, i) {
  const d = st.draft;
  const fields = [];
  if (s.type === "MARKET") {
    fields.push(select_(ANCHORS, s.anchor, (v) => (s.anchor = v)),
      "offset", numInput(s, "offsetMinutes", { style: "width:70px" }), "min",
      "· repeat every", numInput(s, "repeatEveryMinutes", { placeholder: "—", style: "width:70px" }), "min");
  } else if (s.type === "DAILY") {
    fields.push("at", timesInput(s));
  } else if (s.type === "WEEKLY") {
    fields.push("every", numInput(s, "everyNWeeks", { min: 1, style: "width:50px" }), "week(s) on", dayChips(s, "onDays"),
      "at", timesInput(s));
  } else if (s.type === "INTERVAL") {
    s.window ??= null;
    fields.push("every", numInput(s, "every", { min: 1, style: "width:60px" }),
      select_(["MINUTE", "HOUR", "DAY"], s.unit, (v) => (s.unit = v)), dayChips(s, "onDays"),
      "between", el("input", { type: "time", value: s.window?.start?.slice(0, 5) ?? "",
        onchange: (e) => (s.window = e.target.value ? { ...(s.window || { end: "16:00" }), start: e.target.value } : null) }),
      "and", el("input", { type: "time", value: s.window?.end?.slice(0, 5) ?? "",
        onchange: (e) => (s.window = e.target.value ? { ...(s.window || { start: "09:30" }), end: e.target.value } : null) }));
  } else if (s.type === "ONCE") {
    const at = s.at ? new Date(s.at) : null;  // shown in the browser's local time
    const local = at ? new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
    fields.push("at", el("input", { type: "datetime-local", value: local,
      onchange: (e) => (s.at = new Date(e.target.value).toISOString()) }), el("span", { class: "muted" }, "(your browser's time)"));
  }
  const common = [];
  if (s.type !== "MARKET" && s.type !== "ONCE") {
    common.push(select_(ZONES, s.timezone || "America/New_York", (v) => (s.timezone = v), { title: "Time zone" }));
  }
  common.push(select_([["RUN_ONCE", "missed: run once"], ["SKIP", "missed: skip"], ["RUN_ALL", "missed: run all"]],
    s.missedPolicy || "RUN_ONCE", (v) => (s.missedPolicy = v), { title: "If the app was down at run time" }));
  return el("div", { class: "row" },
    el("span", { class: "num" }, `${i + 1}.`),
    select_(Object.keys(SCHEDULE_DEFAULTS), s.type, (v) => {
      d.schedules[i] = { type: v, id: s.id, ...structuredClone(SCHEDULE_DEFAULTS[v]) };
      if (v === "ONCE") d.schedules[i].at = new Date(Date.now() + 3600e3).toISOString();
      rerender();
    }),
    ...fields, ...common,
    el("span", { class: "grow" }),
    el("button", { class: "link", title: "Remove", onclick: () => { d.schedules.splice(i, 1); rerender(); } }, "✕"));
}

function targetRow(t, i) {
  const d = st.draft;
  const options = t.type === "FILTER" ? st.filters : st.groups;
  return el("div", { class: "row" },
    select_([["FILTER", "Filter"], ["FILTER_GROUP", "Filter group"]], t.type, (v) => {
      d.runTargets[i] = { type: v, id: (v === "FILTER" ? st.filters : st.groups)[0]?.id || "" };
      rerender();
    }),
    select_(options.map((o) => [o.id, o.name]), t.id, (v) => (t.id = v), { class: "grow" }),
    el("button", { class: "link", onclick: () => { d.runTargets.splice(i, 1); rerender(); } }, "✕"));
}

function orderActionRow(a, i) {
  const d = st.draft;
  const sel = a.selection;
  const sizingModes = a.side === "BUY" ? ["CASH_AMOUNT", "QUANTITY", "PERCENT_OF_BUDGET"] : ["PERCENT_OF_POSITION", "QUANTITY"];
  const wantSizings = sel.mode === "TOP_N" ? Math.max(1, a.sizings.length) : 1;
  const sizingRow = (z, k) => el("span", {}, sel.mode === "TOP_N" && a.sizings.length > 1 ? `#${k + 1} ` : "",
    select_(sizingModes, z.mode, (v) => (z.mode = v)), numInput(z, "value", { style: "width:80px", min: 0 }));
  return el("div", { class: "row" },
    el("span", { class: "num" }, `${i + 1}.`), el("span", { class: "title" }, "Place order"),
    select_([["BUY", "Buy"], ["SELL", "Sell (own shares)"]], a.side, (v) => {
      a.side = v; a.sizings = [{ mode: v === "BUY" ? "CASH_AMOUNT" : "PERCENT_OF_POSITION", value: v === "BUY" ? 500 : 100 }]; rerender();
    }),
    select_(["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"], a.orderType, (v) => { a.orderType = v; rerender(); }),
    ["LIMIT", "STOP_LIMIT"].includes(a.orderType) ? ["limit offset %", numInput(a, "limitOffsetPercent", { style: "width:60px", step: "0.1" })] : null,
    ["STOP", "STOP_LIMIT"].includes(a.orderType) ? ["stop offset %", numInput(a, "stopOffsetPercent", { style: "width:60px", step: "0.1" })] : null,
    a.orderType === "TRAILING_STOP" ? ["trail %", numInput(a, "trailPercent", { style: "width:60px", step: "0.1" })] : null,
    select_(["DAY", "GTC", "IOC", "FOK", "OPG", "CLS"], a.timeInForce, (v) => (a.timeInForce = v)),
    el("span", { class: "grow" }),
    el("button", { class: "link", onclick: () => { d.actions.splice(i, 1); rerender(); } }, "✕"),
    el("div", { style: "flex-basis:100%" }),
    select_([["ALL_MATCHES", "all matches"], ["TOP_N", "top N"]], sel.mode, (v) => {
      a.selection = v === "TOP_N" ? { mode: v, topN: 3, rankBy: "DAILY_CHANGE_PERCENT", descending: true } : { mode: v };
      a.sizings = a.sizings.slice(0, 1); rerender();
    }),
    sel.mode === "TOP_N" ? [numInput(sel, "topN", { style: "width:50px", min: 1 }), "by",
      select_(RANK_KEYS, sel.rankBy, (v) => (sel.rankBy = v)),
      select_([["true", "highest first"], ["false", "lowest first"]], String(sel.descending), (v) => (sel.descending = v === "true"))] : null,
    el("div", { style: "flex-basis:100%" }),
    "size", ...a.sizings.map(sizingRow),
    sel.mode === "TOP_N" ? el("button", { class: "link", onclick: () => {
      a.sizings = a.sizings.length > 1 ? a.sizings.slice(0, 1)
        : Array.from({ length: sel.topN || 1 }, () => structuredClone(a.sizings[0]));
      rerender();
    } }, a.sizings.length > 1 ? "same size for all ranks" : "size per rank") : null);
}

function guardsEditor() {
  const g = (st.draft.orderGuards ??= structuredClone(GUARD_DEFAULT));
  const check = (key, label) => el("label", {}, el("input", { type: "checkbox", checked: g[key], onchange: (e) => (g[key] = e.target.checked) }), ` ${label}`);
  return el("div", { class: "row" },
    "max orders/run", numInput(g, "maxOrdersPerRun", { style: "width:60px", min: 1 }),
    "max per symbol $", numInput(g, "maxPositionValue", { style: "width:80px", placeholder: "—" }),
    "or %", numInput(g, "maxPositionPercent", { style: "width:60px", placeholder: "—" }),
    "max per sector %", numInput(g, "maxSectorPercent", { style: "width:50px", placeholder: "—" }),
    "max correlation", numInput(g, "maxCorrelation", { style: "width:50px", placeholder: "—", step: 0.05 }),
    "max positions", numInput(g, "maxOpenPositions", { style: "width:50px", placeholder: "—", min: 1 }),
    "size by volatility, target %", numInput(g, "targetVolatilityPercent", { style: "width:50px", placeholder: "—" }),
    "max data age", numInput(g, "maxDataAgeMinutes", { style: "width:50px", min: 1 }), "min",
    "max buy spread %", numInput(g, "maxSpreadPercent", { style: "width:50px", placeholder: "—" }),
    "no re-buy for", numInput(g, "rebuyCooldownMinutes", { style: "width:60px", placeholder: "—", min: 1 }), "min after a sell",
    check("regularHoursOnly", "regular hours only"), check("skipBuyIfHeld", "skip buy if held"),
    check("requireLivePrice", "require live price"));
}

function actionRow(a, i) {
  if (a.type === "PLACE_ORDER") return orderActionRow(a, i);
  const d = st.draft;
  const check = (key, label) => el("label", {}, el("input", { type: "checkbox", checked: a[key],
    onchange: (e) => (a[key] = e.target.checked) }), ` ${label}`);
  return el("div", { class: "row" },
    el("span", { class: "num" }, `${i + 1}.`), el("span", { class: "title" }, "Notify"),
    textInput(a, "title", { placeholder: "Title ({name}, {count})", class: "grow" }),
    textInput(a, "message", { placeholder: "Message", class: "grow" }),
    check("includeMatchedSymbols", "list symbols"), check("skipWhenNoMatches", "skip if none"),
    el("button", { class: "link", onclick: () => { d.actions.splice(i, 1); rerender(); } }, "✕"));
}

function renderEditor() {
  const d = st.draft;
  if (!d) return;
  const saved = Boolean(st.selectedId);
  const paused = d.status === "PAUSED";
  setChildren($("#editor"), 
    el("div", { class: "section-head" }, el("h2", {}, "Automation"),
      el("span", { class: "muted" }, saved ? `${st.selectedId} · v${d.configVersion}` : "not saved")),
    saved ? el("p", { class: "next-run" }, nextRunText(d),
      d.lastRun ? ` · last run ${when(d.lastRun.ranAt)} (${d.lastRun.status.toLowerCase()})` : "") : null,
    el("label", { class: "field" }, el("span", {}, "Name"), textInput(d, "name")),
    el("label", { class: "field" }, el("span", {}, "Description"), textInput(d, "description")),
    el("h3", {}, "Run filters ", el("span", { class: "muted" }, "(matched symbols = union)")),
    ...d.runTargets.map(targetRow),
    el("button", { onclick: () => { d.runTargets.push({ type: "FILTER", id: st.filters[0]?.id || "" }); rerender(); } }, "+ Add filter"),
    el("h3", {}, "Schedules"),
    ...d.schedules.map(scheduleRow),
    el("button", { onclick: () => { d.schedules.push({ type: "DAILY", ...structuredClone(SCHEDULE_DEFAULTS.DAILY) }); rerender(); } }, "+ Add schedule"),
    el("h3", {}, "Budgets ", el("span", { class: "muted" }, "(where its orders take money and shares from)")),
    budgetsLine(d),
    el("h3", {}, "Actions ", el("span", { class: "muted" }, "(in order)")),
    ...d.actions.map(actionRow),
    select_([["", "+ Add action…"], ["NOTIFY", "Notify"], ["PLACE_ORDER", "Place order"], ["ADD_TO_WATCHLIST", "Add to watchlist (phase 3)"]], "",
      (v) => {
        if (v === "NOTIFY") d.actions.push({ type: "NOTIFY", title: "{name}: {count} matches", message: "", includeMatchedSymbols: true, skipWhenNoMatches: false });
        else if (v === "PLACE_ORDER") d.actions.push(structuredClone(ORDER_DEFAULT));
        else if (v) d.actions.push({ type: v });  // the API explains these are not available yet
        rerender();
      }),
    d.actions.some((a) => a.type === "PLACE_ORDER") ? [el("h3", {}, "Order guards"), guardsEditor()] : null,
    el("div", { class: "toolbar" },
      el("button", { onclick: preview }, "Preview"),
      el("button", { onclick: nextRuns }, "Next runs"),
      el("button", { class: "primary", onclick: save }, saved ? "Save" : "Create"),
      el("button", { onclick: runNow, disabled: !saved }, "Run now"),
      el("button", { onclick: togglePause, disabled: !saved }, paused ? "Resume" : "Pause"),
      el("button", { onclick: history, disabled: !saved }, "History"),
      el("button", { onclick: changes, disabled: !saved }, "Changes"),
      el("button", { onclick: recordView, disabled: !saved }, "Record"),
      el("button", { class: "danger", onclick: remove, disabled: !saved }, "Delete")),
  );
}

function budgetsLine(d) {
  const portfolioName = (id) => st.portfolios.find((p) => p.id === id)?.name || id;
  const budgets = d.budgets || [];
  const hint = el("a", { href: "portfolio.html" }, "Portfolio page");
  if (!budgets.length) {
    return el("p", { class: "muted" }, "Not in any budget: order actions are skipped. Add it to a budget on the ", hint, ".");
  }
  return el("p", {}, budgets.map((b) => `${b.name} (${portfolioName(b.portfolioId)})`).join(" · "),
    el("span", { class: "muted" }, " · managed on the "), hint);
}

function body() {
  const d = st.draft;
  const trades = d.actions.some((a) => a.type === "PLACE_ORDER");
  return { name: d.name, description: d.description,
    orderGuards: trades ? d.orderGuards : null, runTargets: d.runTargets, schedules: d.schedules, actions: d.actions };
}

// ------------------------------------------------------------------ actions

const save = () => guard(async () => {
  const saved = st.selectedId
    ? await api(`/v1/automations/${st.selectedId}`, { method: "PUT", body: body() })
    : await api("/v1/automations", { method: "POST", body: body() });
  await loadAll();
  select(saved.id);
  await nextRuns();
});

const preview = () => guard(async () => {
  const out = st.selectedId
    ? await api(`/v1/automations/${st.selectedId}/preview`, { method: "POST" })
    : await api("/v1/automations/preview", { method: "POST", body: body() });
  showRun({ ...out, ranAt: new Date().toISOString(), id: "preview (not stored)" });
});

const runNow = () => guard(async () => {
  showRun(await api(`/v1/automations/${st.selectedId}/run`, { method: "POST" }));
  refreshBell();
});

const togglePause = () => guard(async () => {
  const verb = st.draft.status === "PAUSED" ? "resume" : "pause";
  await api(`/v1/automations/${st.selectedId}/${verb}`, { method: "POST" });
  await loadAll();
  select(st.selectedId);
});

const remove = () => guard(async () => {
  if (!confirm(`Delete "${st.draft.name}"? It leaves its budgets; their money and shares stay. Its run history is kept.`)) return;
  await api(`/v1/automations/${st.selectedId}`, { method: "DELETE" });
  st.selectedId = null; st.draft = null;
  setChildren($("#editor"), el("p", { class: "muted empty" }, "Deleted."));
  await loadAll();
});

const nextRuns = () => guard(async () => {
  const out = st.selectedId
    ? await api(`/v1/automations/${st.selectedId}/next-runs?count=5`)
    : await api("/v1/automations/schedule-preview?count=5", { method: "POST", body: { schedules: st.draft.schedules } });
  showResults(el("h2", {}, "Next runs"),
    out.status === "PAUSED" ? el("p", { class: "muted" }, "Paused: no upcoming runs.") : null,
    ...out.schedules.map((s, i) => el("div", {}, el("h3", {}, `Schedule ${i + 1} · ${s.type}`),
      s.next.length ? el("ul", {}, ...s.next.map((t) => el("li", {}, when(t)))) : el("p", { class: "muted" }, "No upcoming runs."))));
});

const history = () => guard(async () => {
  const { runs } = await api(`/v1/automations/${st.selectedId}/runs?limit=50`);
  showResults(el("h2", {}, `Run history · ${runs.length}`), el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Ran at"), el("th", {}, "Status"), el("th", { class: "num" }, "ms"))),
    el("tbody", {}, ...runs.map((r) => el("tr", { onclick: () => guard(async () => showRun(await api(`/v1/runs/${r.id}`))) },
      el("td", {}, when(r.ranAt)), el("td", {}, el("span", { class: `badge ${r.status}` }, r.status), r.error ? ` ${r.error}` : ""),
      el("td", { class: "num" }, r.durationMs))))));
});

const recordView = () => guard(async () => {
  const { total, budgets } = await api(`/v1/automations/${st.selectedId}/record`);
  const cols = [["Budget", (x) => x.name || "(deleted)"], ["Orders", (x) => x.ordersPlaced], ["Open", (x) => x.openOrders],
    ["Bought", (x) => `${x.buyFills} · ${money(x.spent)}`], ["Sold", (x) => `${x.sellFills} · ${money(x.received)}`],
    ["Realized P&L", (x) => el("span", { class: tone(x.realizedPnL) }, signed(x.realizedPnL))], ["Fees", (x) => money(x.fees)]];
  showResults(
    el("h2", {}, "Record"),
    el("p", { class: "muted" }, "What this automation did with each budget: its own orders and fills. Budget money and history are on the Portfolio page."),
    budgets.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...cols.map(([h], i) => el("th", { class: i ? "num" : "" }, h)))),
      el("tbody", {}, ...[...budgets, { ...total, name: "Total" }].map((x) => el("tr", {},
        ...cols.map(([, f], i) => el("td", { class: i ? "num" : "" }, f(x))))))) : el("p", { class: "muted" }, "No orders yet."));
});

const changes = () => guard(async () => {
  const { events } = await api(`/v1/automations/${st.selectedId}/events`);
  showResults(el("h2", {}, "Changes"), ...events.map((e) => el("div", { class: "note" },
    el("b", {}, e.type.toLowerCase()), ` · v${e.configVersion} · ${when(e.createdAt)}`,
    Object.keys(e.changes).length && e.type === "UPDATED" ? el("div", { class: "muted" }, `changed: ${Object.keys(e.changes).join(", ")}`) : null)));
});

// ------------------------------------------------------------------ results

function showResults(...nodes) {
  setChildren($("#results"), ...nodes.filter(Boolean));
}

function showRun(run) {
  const r = run.result || {};
  const funnel = (steps) => el("div", { class: "funnel" }, ...steps.flatMap((s, i) => [
    i ? el("span", { class: "muted" }, "→") : null,
    el("span", { class: "step" }, s.step.replace("_", " ").toLowerCase(), el("b", {}, s.count))]));
  const m = r.market || {};
  showResults(
    el("div", { class: "section-head" }, el("h2", {}, run.preview ? "Preview (nothing stored or sent)" : "Run"),
      el("span", { class: `badge ${run.status}` }, run.status)),
    el("dl", { class: "kv" },
      el("dt", {}, "When"), el("dd", {}, when(run.ranAt)),
      el("dt", {}, "Trigger"), el("dd", {}, `${r.trigger?.type || "—"}${r.trigger?.scheduledFor ? ` for ${when(r.trigger.scheduledFor)}` : ""}`),
      r.skipReason ? el("dt", {}, "Skipped") : null, r.skipReason ? el("dd", {}, `${r.skipReason}${r.missedCount ? ` (${r.missedCount} times)` : ""}`) : null,
      el("dt", {}, "Market"), el("dd", {}, m.session ? `${m.session} · data ${m.dataAgeMinutes ?? "?"} min old` : "—"),
      el("dt", {}, "Version"), el("dd", {}, `v${r.configVersion ?? "—"}`),
      run.error ? el("dt", {}, "Error") : null, run.error ? el("dd", { class: "down" }, run.error) : null),
    ...(r.targets || []).map((t) => el("div", {}, el("b", {}, `${t.name} (${t.count})`),
      t.steps ? funnel(t.steps) : (t.filters || []).map((f) => el("div", {}, f.name, funnel(f.steps))))),
    ...(r.actions || []).map((a) => {
      const n = a.wouldNotify;
      return el("div", { class: "note" }, el("b", {}, `${a.index + 1}. ${a.type.toLowerCase()} · ${a.status.toLowerCase()}`),
        n ? el("div", {}, `would notify: "${n.title}" ${n.message} ${n.symbols.join(", ")}`) : null,
        a.notificationId ? el("div", { class: "muted" }, "notification sent to inbox") : null,
        a.error ? el("div", { class: "down" }, a.error) : null);
    }),
    ...(r.actions || []).filter((a) => a.detail).map((a) => el("p", { class: "muted" }, `${a.index + 1}. ${a.detail}`)),
    ...(r.actions || []).flatMap((a) => (a.budgets || []).map((b) => el("div", {},
      el("h3", {}, `${a.index + 1}. ${a.side === "SELL" ? "Sell" : "Buy"} orders · ${b.name}`,
        el("span", { class: "muted" }, ` · ${b.intents.filter((x) => x.decision === "PLACE").length} placed${b.notHeld ? ` · ${b.notHeld} matches not held` : ""}${b.alreadyHeld?.length ? ` · already held, left out: ${b.alreadyHeld.join(", ")}` : ""}${b.recentlySold?.length ? ` · sold recently, left out: ${b.recentlySold.join(", ")}` : ""}${b.sectorFull?.length ? ` · sector full, left out: ${b.sectorFull.join(", ")}` : ""}${b.tooCorrelated?.length ? ` · moves with a holding, left out: ${b.tooCorrelated.join(", ")}` : ""}`)),
      b.error ? el("div", { class: "down" }, b.error) : null,
      b.intents.length ? el("table", {},
        el("thead", {}, el("tr", {}, ...["#", "Symbol", "Qty", "Price", "Cost", "Decision", "Why"].map((h) => el("th", {}, h)))),
        el("tbody", {}, ...b.intents.map((x) => el("tr", { title: x.orderId || "" },
          el("td", {}, x.rank ?? ""), el("td", {}, el("b", {}, x.ticker)), el("td", { class: "num" }, fmt(x.quantity, 0)),
          el("td", { class: "num", title: ["ALPACA", "IBKR"].includes(x.priceSource) ? `live quote, ${x.quoteAgeSeconds ?? "?"} s old` : "newest minute bar (no live quote)" },
            money(x.limitPrice ?? x.estimatedPrice), el("span", { class: "muted" }, ["ALPACA", "IBKR"].includes(x.priceSource) ? " live" : x.priceSource ? " bar" : "")),
          el("td", { class: "num" }, money(x.estimatedCost)),
          el("td", {}, el("span", { class: `badge ${x.decision === "PLACE" ? "SUCCEEDED" : x.decision === "FAILED" ? "FAILED" : "SKIPPED"}` },
            x.decision === "PLACE" && run.preview ? "would place" : x.orderStatus ? x.orderStatus.toLowerCase() : x.decision.toLowerCase())),
          el("td", { class: "muted" }, [x.skipReason, x.detail].filter(Boolean).join(": ")))))) : null))),
    r.liveQuotes ? el("p", { class: "muted" }, `Live prices: ${r.liveQuotes.live} of ${r.liveQuotes.asked} symbols${r.liveQuotes.error ? ` (live quotes failed: ${r.liveQuotes.error})` : ""}`) : null,
    ...(r.budgets || []).filter((b) => b.before).map((b) =>
      el("p", { class: "muted" }, `${b.name}: available ${money(b.before.available)} → ${money(b.after?.available)}`)),
    (r.matched || []).length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Ticker"), el("th", {}, "Name"), el("th", { class: "num" }, "Price"), el("th", { class: "num" }, "Day %"))),
      el("tbody", {}, ...r.matched.map((s) => el("tr", {}, el("td", {}, el("b", {}, s.ticker)), el("td", {}, (s.name || "").slice(0, 36)),
        el("td", { class: "num" }, fmt(s.price)), el("td", { class: "num" }, fmt(s.dailyChangePercent)))))) : null,
  );
}

async function showInbox() {
  const { notifications, unread } = await api("/v1/notifications?limit=100");
  showResults(
    el("div", { class: "section-head" }, el("h2", {}, `Inbox · ${unread} unread`),
      el("button", { onclick: async () => { await api("/v1/notifications/read-all", { method: "POST" }); refreshBell(); showInbox(); } }, "Mark all read")),
    ...notifications.map((n) => el("div", { class: `note ${n.readAt ? "" : "unread"}`, onclick: async () => {
      if (!n.readAt) { await api(`/v1/notifications/${n.id}/read`, { method: "POST" }); refreshBell(); }
      if (n.runId) showRun(await api(`/v1/runs/${n.runId}`));
    } },
      el("div", {}, n.title), el("div", { class: "muted" }, `${when(n.createdAt)} ${n.message}`),
      n.symbols.length ? el("div", {}, n.symbols.join(", ")) : null)),
    notifications.length ? null : el("p", { class: "muted" }, "No notifications yet."));
}

// ------------------------------------------------------------------ start

$("#new-automation").onclick = () => select(null);
$("#show-inbox").onclick = () => showInbox();

setInterval(async () => {
  try {
    st.automations = (await api("/v1/automations")).automations;
    renderSidebar();
  } catch {}
}, 60000);

start("automations.html", async () => {
  await loadAll();
  if (location.hash === "#inbox") await showInbox();
});
