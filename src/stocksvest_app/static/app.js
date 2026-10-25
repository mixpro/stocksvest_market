// Filters page: exercises every filter, group and run endpoint of the app API. Helpers are in common.js.

const state = {
  meta: null,
  filters: [],
  groups: [],
  universe: {},          // ticker -> symbol
  selected: null,        // {kind: "filter" | "group", id: string | null}
  draft: null,           // the object being edited
  result: null,          // last preview / run shown on the right
  chartTicker: null,
};

// ------------------------------------------------------------------ loading

async function loadAll() {
  const [meta, filters, groups] = await Promise.all([
    api("/v1/filter-actions"), api("/v1/filters"), api("/v1/filter-groups"),
  ]);
  state.meta = meta;
  state.filters = filters.filters;
  state.groups = groups.groups;
  renderSidebar();
}

async function loadUniverse() {
  try {
    const symbols = await api("/v1/market/symbols?limit=50000");
    state.universe = Object.fromEntries(symbols.symbols.map((s) => [s.ticker, s]));
  } catch {}
}

// ------------------------------------------------------------------ sidebar

function renderSidebar() {
  const item = (kind, obj, tag) =>
    el("li", {
      class: state.selected && state.selected.kind === kind && state.selected.id === obj.id ? "active" : "",
      onclick: () => select(kind, obj.id),
    }, el("span", {}, obj.name), tag ? el("span", { class: "tag" }, tag) : null);
  setChildren($("#filter-list"), ...state.filters.map((f) => item("filter", f, f.isActive ? "" : "inactive")));
  setChildren($("#group-list"), ...state.groups.map((g) => item("group", g, `${g.filterIds.length}`)));
}

function select(kind, id) {
  state.selected = { kind, id };
  const source = kind === "filter" ? state.filters : state.groups;
  const existing = source.find((x) => x.id === id);
  state.draft = existing
    ? structuredClone(existing)
    : kind === "filter"
      ? { name: "New filter", description: "", isActive: true, inputs: [{ type: "UNIVERSE" }], actions: [] }
      : { name: "New group", description: "", filterIds: [] };
  state.result = null;
  renderSidebar();
  renderEditor();
  renderResults();
}

// ------------------------------------------------------------------ editor

function setError(message) {
  const box = $("#editor .error");
  if (box) box.remove();
  if (message) $("#editor").prepend(el("div", { class: "error" }, message));
}

function textField(label, key, multiline = false) {
  const input = multiline
    ? el("textarea", { rows: 2, oninput: (e) => (state.draft[key] = e.target.value) })
    : el("input", { type: "text", value: state.draft[key], oninput: (e) => (state.draft[key] = e.target.value) });
  if (multiline) input.value = state.draft[key];
  return el("label", { class: "field" }, el("span", {}, label), input);
}

function renderEditor() {
  const box = $("#editor");
  if (!state.draft) return;
  const isFilter = state.selected.kind === "filter";
  const saved = Boolean(state.selected.id);
  setChildren(box,
    el("div", { class: "section-head" }, el("h2", {}, isFilter ? "Filter" : "Filter group"),
      saved ? el("span", { class: "muted" }, state.selected.id) : el("span", { class: "muted" }, "not saved")),
    textField("Name", "name"),
    textField("Description", "description", true),
    ...(isFilter ? filterEditor() : groupEditor()),
    el("div", { class: "toolbar" },
      isFilter ? el("button", { onclick: preview }, "Preview") : null,
      el("button", { class: "primary", onclick: save }, saved ? "Save" : "Create"),
      el("button", { onclick: runStored, disabled: !saved }, "Run & store"),
      el("button", { onclick: showHistory, disabled: !saved }, "History"),
      el("button", { class: "danger", onclick: remove, disabled: !saved }, "Delete"),
    ),
  );
}

function filterEditor() {
  const d = state.draft;
  return [
    el("label", { class: "field" },
      el("input", { type: "checkbox", checked: d.isActive, onchange: (e) => (d.isActive = e.target.checked) }),
      " Active (used by automations later)"),
    el("h3", {}, "Inputs ", el("span", { class: "muted" }, "(union)")),
    ...d.inputs.map((inp, i) => inputRow(inp, i)),
    el("button", { onclick: () => { d.inputs.push({ type: "UNIVERSE" }); renderEditor(); } }, "+ Add input"),
    el("h3", {}, "Actions ", el("span", { class: "muted" }, "(applied top to bottom)")),
    ...d.actions.map((a, i) => actionRow(a, i)),
    addActionSelect(),
  ];
}

function inputRow(inp, i) {
  const d = state.draft;
  const typeSelect = el("select", {
    onchange: (e) => { d.inputs[i] = { type: e.target.value, id: e.target.value === "UNIVERSE" ? undefined : "" }; renderEditor(); },
  }, ...state.meta.inputTypes.map((t) =>
    el("option", { value: t.type, selected: t.type === inp.type, disabled: !t.available }, t.available ? t.type : `${t.type} (soon)`)));

  let idSelect = null;
  if (inp.type === "FILTER" || inp.type === "FILTER_GROUP" || inp.type === "INVESTMENT_PORTFOLIO") {
    const portfolios = (state.meta.inputTypes.find((t) => t.type === "INVESTMENT_PORTFOLIO")?.options || [])
      .map((o) => ({ id: o.value, name: o.label }));
    const options = inp.type === "FILTER"
      ? state.filters.filter((f) => f.id !== state.selected.id)
      : inp.type === "FILTER_GROUP" ? state.groups : portfolios;
    idSelect = el("select", { class: "grow", onchange: (e) => (inp.id = e.target.value) },
      el("option", { value: "" }, "— choose —"),
      ...options.map((o) => el("option", { value: o.id, selected: o.id === inp.id }, o.name)));
  }
  return el("div", { class: "row" }, typeSelect, idSelect,
    el("button", { class: "link", onclick: () => { d.inputs.splice(i, 1); renderEditor(); } }, "✕"));
}

function actionMeta(type) {
  return state.meta.actions.find((a) => a.type === type);
}

function actionRow(action, i) {
  const d = state.draft;
  const meta = actionMeta(action.type);
  const move = (delta) => {
    const j = i + delta;
    if (j < 0 || j >= d.actions.length) return;
    [d.actions[i], d.actions[j]] = [d.actions[j], d.actions[i]];
    renderEditor();
  };
  const fields = meta.fields
    .filter((f) => !f.requiredWhen || Object.entries(f.requiredWhen).every(([k, v]) => action[k] === v))
    .map((f) => fieldInput(action, f));
  return el("div", { class: "row" },
    el("span", { class: "num" }, `${i + 1}.`),
    el("span", { class: "title" }, meta.label),
    ...fields,
    el("span", { class: "grow" }),
    el("button", { class: "link", title: "Move up", onclick: () => move(-1) }, "↑"),
    el("button", { class: "link", title: "Move down", onclick: () => move(1) }, "↓"),
    el("button", { class: "link", title: "Remove", onclick: () => { d.actions.splice(i, 1); renderEditor(); } }, "✕"));
}

function fieldInput(action, f) {
  const opts = (f.options || []).map((o) => (typeof o === "string" ? { value: o, label: o } : o));
  if (f.kind === "select") {
    return el("select", {
      title: f.name,
      onchange: (e) => {
        action[f.name] = e.target.value;
        if (f.name === "condition" && e.target.value !== "BETWEEN") delete action.maxValue;
        renderEditor();
      },
    }, ...opts.map((o) => el("option", { value: o.value, selected: action[f.name] === o.value }, o.label)));
  }
  if (f.kind === "multiselect") {
    const current = new Set(action[f.name] || []);
    return el("span", { class: "chips" }, ...opts.map((o) =>
      el("label", {}, el("input", {
        type: "checkbox", checked: current.has(o.value),
        onchange: (e) => {
          e.target.checked ? current.add(o.value) : current.delete(o.value);
          action[f.name] = [...current];
        },
      }), o.label)));
  }
  if (f.kind === "symbols") {
    return el("input", {
      type: "text", title: f.name, placeholder: "GOOG, ...", value: (action[f.name] || []).join(", "),
      oninput: (e) => { action[f.name] = e.target.value.split(/[\s,]+/).filter(Boolean).map((t) => t.toUpperCase()); },
    });
  }
  const isInt = f.kind === "integer";
  return el("input", {
    type: "number", title: f.name, placeholder: f.name, step: isInt ? 1 : "any", min: f.min,
    value: action[f.name] ?? "",
    oninput: (e) => {
      const v = e.target.value === "" ? undefined : isInt ? parseInt(e.target.value, 10) : parseFloat(e.target.value);
      action[f.name] = v;
    },
  });
}

const ACTION_DEFAULTS = {
  SYMBOL_TYPE: { assetTypes: ["COMMON_STOCK"] },
  SECTOR: { sectorIds: [] },
  EXCLUDE_SYMBOLS: { tickers: [] },
  SYMBOL_PRICE: { priceReference: "LATEST_BAR", condition: "BETWEEN", value: 10, maxValue: 100 },
  PRICE_CHANGE: { periodAmount: 5, periodUnit: "DAYS", valueType: "PERCENT", condition: "MORE_THAN", value: 3 },
  MARKET_CHANGE: { periodAmount: 6, periodUnit: "MONTHS", condition: "MORE_THAN", value: 0 },
  AVERAGE_VOLUME: { days: 20, volumeType: "DOLLARS", condition: "MORE_THAN", value: 1000000 },
  POSITION: { portfolioIds: [] },
  UNREALIZED_PNL: { portfolioIds: [], valueType: "PERCENT", condition: "LESS_THAN", value: -8 },
  HOLDING_PERIOD: { portfolioIds: [], periodUnit: "DAYS", condition: "MORE_THAN", value: 5 },
};

function addActionSelect() {
  return el("select", {
    onchange: (e) => {
      if (!e.target.value) return;
      state.draft.actions.push({ type: e.target.value, ...structuredClone(ACTION_DEFAULTS[e.target.value]) });
      renderEditor();
    },
  }, el("option", { value: "" }, "+ Add action…"),
    ...state.meta.actions.map((a) =>
      el("option", { value: a.type, disabled: !a.available }, a.available ? a.label : `${a.label} (${a.fields.some((f) => f.name === "portfolioIds") ? "no portfolios yet" : "no data yet"})`)));
}

function groupEditor() {
  const d = state.draft;
  const members = new Set(d.filterIds);
  return [
    el("h3", {}, "Member filters ", el("span", { class: "muted" }, "(result = union)")),
    ...state.filters.map((f) => el("label", { class: "field" },
      el("input", {
        type: "checkbox", checked: members.has(f.id),
        onchange: (e) => {
          d.filterIds = e.target.checked ? [...d.filterIds, f.id] : d.filterIds.filter((x) => x !== f.id);
        },
      }), ` ${f.name}`)),
    state.filters.length ? null : el("p", { class: "muted" }, "Create filters first."),
  ];
}

// ------------------------------------------------------------------ actions

function filterBody() {
  const d = state.draft;
  return { name: d.name, description: d.description, isActive: d.isActive, inputs: d.inputs, actions: d.actions };
}

async function guard(fn) {
  setError(null);
  try { await fn(); } catch (e) { setError(e.message); }
}

const preview = () => guard(async () => {
  const out = await api("/v1/filters/preview", { method: "POST", body: filterBody() });
  state.result = { title: "Preview (not stored)", ...out };
  renderResults();
});

const save = () => guard(async () => {
  const isFilter = state.selected.kind === "filter";
  const base = isFilter ? "/v1/filters" : "/v1/filter-groups";
  const body = isFilter ? filterBody() : { name: state.draft.name, description: state.draft.description, filterIds: state.draft.filterIds };
  const saved = state.selected.id
    ? await api(`${base}/${state.selected.id}`, { method: "PUT", body })
    : await api(base, { method: "POST", body });
  await loadAll();
  select(state.selected.kind, saved.id);
});

const remove = () => guard(async () => {
  if (!confirm(`Delete "${state.draft.name}"?`)) return;
  const base = state.selected.kind === "filter" ? "/v1/filters" : "/v1/filter-groups";
  await api(`${base}/${state.selected.id}`, { method: "DELETE" });
  state.selected = null; state.draft = null; state.result = null;
  setChildren($("#editor"), el("p", { class: "muted empty" }, "Deleted."));
  await loadAll();
  renderResults();
});

const runStored = () => guard(async () => {
  const base = state.selected.kind === "filter" ? "/v1/filters" : "/v1/filter-groups";
  const run = await api(`${base}/${state.selected.id}/run`, { method: "POST" });
  showRun(run);
});

const showHistory = () => guard(async () => {
  const base = state.selected.kind === "filter" ? "/v1/filters" : "/v1/filter-groups";
  const { runs } = await api(`${base}/${state.selected.id}/runs?limit=50`);
  state.result = null;
  setChildren($("#result-head"), el("h2", {}, `Run history · ${runs.length}`));
  setChildren($("#table-wrap"), el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Ran at"), el("th", {}, "Status"), el("th", { class: "num" }, "ms"))),
    el("tbody", {}, ...runs.map((r) => el("tr", { onclick: () => guard(async () => showRun(await api(`/v1/runs/${r.id}`))) },
      el("td", {}, new Date(r.ranAt).toLocaleString()),
      el("td", { class: r.status === "FAILED" ? "down" : "" }, r.error ? `FAILED: ${r.error}` : r.status),
      el("td", { class: "num" }, r.durationMs))))));
});

function showRun(run) {
  if (run.status === "FAILED") { setError(`Run failed: ${run.error}`); return; }
  state.result = { title: `Run ${new Date(run.ranAt).toLocaleString()} · ${run.durationMs} ms`, ...run.result };
  renderResults();
}

// ------------------------------------------------------------------ results


function renderResults() {
  const r = state.result;
  $("#chart-box").hidden = true;
  if (!r) {
    setChildren($("#result-head"), el("p", { class: "muted empty" }, "Preview or run to see matching symbols."));
    setChildren($("#table-wrap"), );
    return;
  }
  const funnel = (steps) => el("div", { class: "funnel" }, ...steps.flatMap((s, i) => [
    i ? el("span", { class: "muted" }, "→") : null,
    el("span", { class: "step" }, s.step.replace("_", " ").toLowerCase(), el("b", {}, s.count)),
  ]));
  setChildren($("#result-head"), 
    el("div", { class: "section-head" }, el("h2", {}, r.title), el("span", { class: "muted" }, `${r.symbols.length} symbols`)),
    r.steps ? funnel(r.steps) : null,
    ...(r.filters || []).map((m) => el("div", {}, el("b", {}, m.name), funnel(m.steps))),
  );
  const sectorName = Object.fromEntries((actionMeta("SECTOR")?.fields[0].options || []).map((o) => [o.value, o.label]));
  setChildren($("#table-wrap"), el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Ticker"), el("th", {}, "Name"), el("th", {}, "Sector"),
      el("th", { class: "num" }, "Price"), el("th", { class: "num" }, "Day %"))),
    el("tbody", {}, ...r.symbols.map((s) => {
      const chg = s.dailyChangePercent;
      return el("tr", { class: s.ticker === state.chartTicker ? "active" : "", onclick: () => showChart(s.ticker) },
        el("td", {}, el("b", {}, s.ticker)),
        el("td", {}, (s.name || "").slice(0, 40)),
        el("td", {}, sectorName[s.sector] || s.sector || "—"),
        el("td", { class: "num" }, fmt(s.price)),
        el("td", { class: `num ${chg > 0 ? "up" : chg < 0 ? "down" : ""}` }, chg === null || chg === undefined ? "—" : `${chg > 0 ? "+" : ""}${fmt(chg)}`));
    }))));
}

// ------------------------------------------------------------------ chart

let chart, candles, volume;

async function showChart(ticker) {
  state.chartTicker = ticker;
  document.querySelectorAll("#table-wrap tbody tr").forEach((tr) => tr.classList.toggle("active", tr.firstChild.textContent === ticker));
  const tf = $("#chart-timeframe").value;
  $("#chart-box").hidden = false;
  $("#chart-title").textContent = `${ticker} · ${state.universe[ticker]?.name || ""}`;
  if (!chart) {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    chart = LightweightCharts.createChart($("#chart"), {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: dark ? "#e6e8eb" : "#1d2330" },
      grid: { vertLines: { visible: false }, horzLines: { color: dark ? "#2b313a" : "#eef0f3" } },
      timeScale: { timeVisible: true },
    });
    candles = chart.addCandlestickSeries();
    volume = chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, color: "#94a3b8" });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  }
  try {
    const { bars } = await api(`/v1/market/bars/${encodeURIComponent(ticker)}?timeframe=${tf}&limit=2000`);
    const t = (b) => Math.floor(new Date(b.t).getTime() / 1000);
    candles.setData(bars.map((b) => ({ time: t(b), open: b.o, high: b.h, low: b.l, close: b.c })));
    volume.setData(bars.map((b) => ({ time: t(b), value: b.v })));
    chart.timeScale().fitContent();
  } catch (e) {
    $("#chart-title").textContent = `${ticker}: ${e.message}`;
  }
}

// ------------------------------------------------------------------ start

$("#new-filter").onclick = () => select("filter", null);
$("#new-group").onclick = () => select("group", null);
$("#chart-timeframe").onchange = () => state.chartTicker && showChart(state.chartTicker);

start("index.html", () => Promise.all([loadAll(), loadUniverse()]));
