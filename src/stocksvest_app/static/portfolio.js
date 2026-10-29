// Portfolio page: broker accounts, portfolios with their capital, history chart, positions (by budget and buyer),
// budgets with their automations, manual orders, orders and fills.

const pf = { accounts: [], portfolios: [], automations: {}, selectedId: null, data: null, range: "1M", returnRange: "3M" };
const RANGES = [["1W", 7], ["1M", 30], ["3M", 90], ["1Y", 365], ["All", null]];
const ALPACA_PERIOD = { "1W": "1W", "1M": "1M", "3M": "3M", "1Y": "1A", All: "1A" };

function setError(message) {
  $("#editor .error")?.remove();
  if (message) $("#editor").prepend(el("div", { class: "error" }, message));
}

async function guard(fn) {
  setError(null);
  try { await fn(); } catch (e) { setError(e.message); }
}

const ownerName = (id) => (id ? pf.automations[id]?.name || id : "manual");
const budgetName = (id) => (id ? pf.data?.budgets.find((b) => b.id === id)?.name || "(deleted budget)" : "—");

// ------------------------------------------------------------------ sidebar

async function loadAll() {
  const [a, p, autos] = await Promise.all([api("/v1/broker-accounts"), api("/v1/portfolios"), api("/v1/automations")]);
  pf.accounts = a.accounts;
  pf.portfolios = p.portfolios;
  pf.automations = Object.fromEntries(autos.automations.map((x) => [x.id, x]));
  setChildren($("#portfolio-list"), ...pf.portfolios.map((x) =>
    el("li", { class: x.id === pf.selectedId ? "active" : "", onclick: () => openPortfolio(x.id) },
      el("span", {}, x.name), el("span", { class: "tag" }, money(x.cash)))));
  setChildren($("#account-list"), ...pf.accounts.map((x) =>
    el("li", { class: "stacked", title: x.lastError || "", onclick: () => guard(() => openAccount(x.id)) },
      el("div", { class: "line" }, el("span", {}, x.name), el("span", { class: `badge ${x.status === "CONNECTED" ? "ACTIVE" : "FAILED"}` }, x.status.toLowerCase())),
      el("div", { class: "sub" }, `${x.provider} · ${x.environment} · keys ${x.credentialsRef}_KEY`))));
  if (!pf.selectedId && pf.portfolios.length) await openPortfolio(pf.portfolios[0].id);
}

$("#new-account").onclick = () => {
  const form = { name: "Alpaca paper", provider: "ALPACA", credentialsRef: "ALPACA" };
  setChildren($("#editor"), 
    el("h2", {}, "New broker account"),
    el("p", { class: "muted" }, "Paper accounts only. Keys are read from .env as <REF>_KEY and <REF>_SECRET; they are never stored."),
    el("label", { class: "field" }, el("span", {}, "Name"), el("input", { type: "text", value: form.name, oninput: (e) => (form.name = e.target.value) })),
    el("label", { class: "field" }, el("span", {}, "Provider"), el("select", { onchange: (e) => (form.provider = e.target.value) },
      el("option", { value: "ALPACA" }, "Alpaca"), el("option", { value: "IBKR", disabled: true }, "IBKR (later)"))),
    el("label", { class: "field" }, el("span", {}, "Credentials ref"), el("input", { type: "text", value: form.credentialsRef, oninput: (e) => (form.credentialsRef = e.target.value) })),
    el("div", { class: "toolbar" }, el("button", { class: "primary", onclick: () => guard(async () => {
      const acct = await api("/v1/broker-accounts", { method: "POST", body: form });
      if (acct.status !== "CONNECTED") throw new Error(`Saved, but connecting failed: ${acct.lastError}`);
      await loadAll();
      setChildren($("#editor"), el("p", {}, `Connected: cash ${money(acct.snapshot.cash)} ${acct.snapshot.currency}. Now create a portfolio.`));
    }) }, "Add and test connection")));
};

$("#new-portfolio").onclick = () => guard(async () => {
  if (!pf.accounts.length) throw new Error("Add a broker account first.");
  const form = { name: "New portfolio", description: "", brokerAccountId: pf.accounts[0].id, capital: 0 };
  const free = el("span", { class: "muted" });
  const showFree = async () => {
    const a = await api(`/v1/broker-accounts/${form.brokerAccountId}`);
    free.textContent = `unassigned in this account: ${money(a.unassignedCash)}`;
  };
  setChildren($("#editor"),
    el("h2", {}, "New portfolio"),
    el("p", { class: "muted" }, "A portfolio gets part of a broker account's cash. Shares already in the account stay unassigned."),
    el("label", { class: "field" }, el("span", {}, "Name"), el("input", { type: "text", value: form.name, oninput: (e) => (form.name = e.target.value) })),
    el("label", { class: "field" }, el("span", {}, "Broker account"), el("select", { onchange: (e) => { form.brokerAccountId = e.target.value; showFree(); } },
      ...pf.accounts.map((a) => el("option", { value: a.id }, a.name)))),
    el("label", { class: "field" }, el("span", {}, "Starting capital"),
      el("input", { type: "number", min: 0, step: "0.01", value: 0, oninput: (e) => (form.capital = Number(e.target.value)) }), " ", free),
    el("div", { class: "toolbar" }, el("button", { class: "primary", onclick: () => guard(async () => {
      const p = await api("/v1/portfolios", { method: "POST", body: form });
      pf.selectedId = p.id;
      await loadAll();
      await openPortfolio(p.id);
    }) }, "Create")));
  await showFree();
});

// ------------------------------------------------------------------ portfolio view

async function openPortfolio(id) {
  pf.selectedId = id;
  pf.data = await api(`/v1/portfolios/${id}`);
  render();
  await Promise.all([showOrders(), drawHistory(), drawReturns()]);
}

// A collapsible chart section (<details>); whether it is open is remembered per browser.
function chartSection(key, title, controls, boxId, draw) {
  let open = true;
  try { open = localStorage.getItem(`svOpen:${key}`) !== "0"; } catch {}
  return el("details", { class: "chart-section", open, ontoggle: (e) => {
    try { localStorage.setItem(`svOpen:${key}`, e.target.open ? "1" : "0"); } catch {}
    if (e.target.open) guard(draw);
  } }, el("summary", {}, el("h3", {}, title)), el("div", { class: "row" }, controls), el("div", { id: boxId, class: "chart-box" }));
}

// Total return % (equity vs capital) over time: one point per close, every 15 minutes on 1W.
async function drawReturns() {
  const box = $("#returns");
  if (!box?.closest("details").open) return;
  const start = sinceDays(Object.fromEntries(RANGES)[pf.returnRange]);
  const query = new URLSearchParams({ ...(start ? { start } : {}), ...(pf.returnRange !== "1W" ? { kind: "CLOSE" } : {}) });
  const { points, current } = await api(`/v1/portfolios/${pf.selectedId}/history?${query}`);
  const pctText = (v) => `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
  lineChart(box, [{ name: "Total return", data: [...points, current].map((p) => ({ at: p.at, value: p.totalReturnPct, gain: p.totalReturn })),
    hover: (p) => `${pctText(p.value)} (${signed(p.gain)})` }], { format: pctText, zeroLine: true });
  if (points.length < 2) box.append(el("p", { class: "muted" }, "Builds up from now: one point after each close (every 15 minutes on 1W)."));
}

async function drawHistory() {
  const box = $("#history");
  if (!box?.closest("details").open) return;
  const start = sinceDays(Object.fromEntries(RANGES)[pf.range]);
  const { points, current } = await api(`/v1/portfolios/${pf.selectedId}/history?${new URLSearchParams(start ? { start } : {})}`);
  const all = [...points, current];
  const series = [
    { name: "Equity", data: all.map((p) => ({ at: p.at, value: p.equity })) },
    { name: "Cash", data: all.map((p) => ({ at: p.at, value: p.cash })) },
  ];
  if ($("#with-account")?.checked) {
    const acct = await api(`/v1/broker-accounts/${pf.data.brokerAccountId}/equity-history?period=${ALPACA_PERIOD[pf.range]}`);
    series.push({ name: "Whole account equity (broker)", data: acct.points.map((p) => ({ at: p.at, value: p.equity })), scale: "left" });
  }
  lineChart(box, series);
  if (points.length < 2) box.append(el("p", { class: "muted" }, "History builds up from now: a point every 15 minutes in the regular session and one after each close."));
}

async function openAccount(id) {
  const a = await api(`/v1/broker-accounts/${id}`);
  const shares = Object.entries(a.unassignedShares || {});
  setChildren($("#editor"),
    el("div", { class: "section-head" }, el("h2", {}, a.name), el("span", { class: `badge ${a.status === "CONNECTED" ? "ACTIVE" : "FAILED"}` }, a.status.toLowerCase())),
    el("dl", { class: "kv" },
      el("dt", {}, "Broker"), el("dd", {}, `${a.provider} · ${a.environment}`),
      el("dt", {}, "Cash"), el("dd", {}, money(a.cash)),
      el("dt", {}, "Equity"), el("dd", {}, money(a.equity)),
      el("dt", {}, "In portfolios"), el("dd", {}, money(a.assignedCash)),
      el("dt", {}, "Unassigned cash"), el("dd", {}, money(a.unassignedCash)),
      el("dt", {}, "Unassigned shares"), el("dd", {}, shares.length ? shares.map(([s, q]) => `${q} ${s}`).join(", ") : "none"),
      el("dt", {}, "Last sync"), el("dd", {}, when(a.lastSync?.ranAt))),
    el("h3", {}, "Portfolios"),
    a.portfolios.length ? el("ul", {}, ...a.portfolios.map((p) => el("li", {}, `${p.name}: ${money(p.cash)}`))) : el("p", { class: "muted" }, "None yet."),
    el("h3", {}, "Account equity (from the broker)"),
    el("div", { id: "account-chart", class: "chart-box" }));
  const hist = await api(`/v1/broker-accounts/${id}/equity-history?period=1A`);
  lineChart($("#account-chart"), [{ name: "Equity", data: hist.points.map((p) => ({ at: p.at, value: p.equity })) }]);
}

function capitalControls(p) {
  const form = { amount: 1000 };
  const move = (verb) => guard(async () => {
    pf.data = await api(`/v1/portfolios/${p.id}/capital/${verb}`, { method: "POST", body: { amount: form.amount } });
    await loadAll();
    render();
    drawHistory();
    drawReturns();
  });
  return el("div", { class: "row" }, "capital",
    el("input", { type: "number", min: 0, step: "0.01", value: form.amount, style: "width:100px", oninput: (e) => (form.amount = Number(e.target.value)) }),
    el("button", { onclick: () => move("allocate") }, "Add from account"),
    el("button", { onclick: () => move("withdraw") }, "Return to account"),
    el("span", { class: "muted" }, `unassigned in account: ${money(p.account.unassignedCash)}`),
    el("span", { class: "grow" }),
    el("button", { class: "link", onclick: () => guard(showLedger) }, "Ledger"),
    el("button", { class: "link danger", onclick: () => guard(async () => {
      if (!confirm(`Delete "${p.name}"? Only an empty portfolio can be deleted; its cash returns to the account.`)) return;
      await api(`/v1/portfolios/${p.id}`, { method: "DELETE" });
      pf.selectedId = null;
      await loadAll();
    }) }, "Delete"));
}

async function showLedger() {
  const { entries } = await api(`/v1/portfolios/${pf.selectedId}/ledger`);
  setChildren($("#results"), el("h2", {}, `Cash ledger · ${pf.data.name}`),
    el("table", {},
      el("thead", {}, el("tr", {}, ...["Time", "Type", "Amount", "Cash after", "Owner", "Note"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...entries.map((e) => el("tr", { title: e.orderId || "" },
        el("td", {}, when(e.createdAt)), el("td", {}, e.type.toLowerCase().replace("_", " ")),
        el("td", { class: `num ${tone(e.amount)}` }, signed(e.amount)), el("td", { class: "num" }, money(e.cashAfter)),
        el("td", {}, [e.budgetId ? budgetName(e.budgetId) : "", e.automationId ? ownerName(e.automationId) : ""].filter(Boolean).join(" · ")),
        el("td", {}, e.note || ""))))),
    el("button", { class: "link", onclick: () => guard(showOrders) }, "Back to orders"));
}

// This year's sold lots as a CSV file (sent with the API key, so it works with or without the login proxy).
async function downloadSales(p) {
  const year = new Date().getFullYear();
  const res = await fetch(`/v1/portfolios/${p.id}/sales?year=${year}`, { headers: { "X-API-Key": apiKey() } });
  if (!res.ok) throw new Error(`${res.status}: sales export failed`);
  const a = el("a", { href: URL.createObjectURL(await res.blob()), download: `${p.name}-sales-${year}.csv` });
  a.click();
  URL.revokeObjectURL(a.href);
}

function render() {
  const p = pf.data;
  const sync = p.lastSync;
  setChildren($("#editor"), 
    el("div", { class: "section-head" }, el("h2", {}, p.name),
      el("button", { onclick: () => guard(async () => { const r = await api(`/v1/portfolios/${p.id}/sync`, { method: "POST" }); pf.data = r.portfolio; render(); showOrders(); drawHistory(); drawReturns(); }) }, "Sync now")),
    el("dl", { class: "kv" },
      el("dt", {}, "Capital"), el("dd", {}, `${money(p.allocated)} ${p.currency} · from ${p.account.name}`),
      el("dt", {}, "Cash"), el("dd", {}, money(p.cash)),
      el("dt", {}, "Unallocated"), el("dd", {}, `${money(p.unallocatedCash)} (not in budgets)`),
      el("dt", {}, "Total return"), el("dd", {}, `${money(p.allocated)} → ${money(p.equity)} · `,
        el("span", { class: tone(p.totalReturn) }, signed(p.totalReturn) + signedPct(p.totalReturnPct)), el("span", { class: "muted" }, " capital → equity")),
      el("dt", {}, "Open positions"), el("dd", {}, `${money(p.positionsCost)} → ${money(p.positionsValue)} · `,
        el("span", { class: tone(p.unrealizedPnL) }, signed(p.unrealizedPnL) + signedPct(p.unrealizedPct)), el("span", { class: "muted" }, " cost → value")),
      el("dt", {}, "Realized P&L"), el("dd", { class: tone(p.realizedPnL) }, signed(p.realizedPnL)),
      el("dt", {}, "Last sync"), el("dd", {}, sync ? `${when(sync.ranAt)}${sync.error ? ` · ${sync.error}` : ""}` : "—"),
      el("dt", {}, "Tax records"), el("dd", {}, el("button", { class: "link", onclick: () => guard(() => downloadSales(p)) },
        `Sales ${new Date().getFullYear()} (CSV)`))),
    capitalControls(p),
    chartSection("returns", "Total return %",
      [rangeButtons(RANGES, pf.returnRange, (d, label) => { pf.returnRange = label; render(); drawHistory(); drawReturns(); })],
      "returns", drawReturns),
    chartSection("history", "History: equity and cash",
      [rangeButtons(RANGES, pf.range, (d, label) => { pf.range = label; render(); drawHistory(); drawReturns(); }),
        el("label", { class: "muted" }, el("input", { type: "checkbox", id: "with-account", onchange: () => guard(drawHistory) }), " whole account")],
      "history", drawHistory),
    sync?.mismatches?.length ? el("div", { class: "error" }, "Differences with the broker:\n" +
      sync.mismatches.map((m) => `${m.kind} ${m.symbol || ""} app ${m.appValue ?? ""} broker ${m.brokerValue ?? ""} ${m.detail || ""}`).join("\n")) : null,
    el("h3", {}, "Positions"),
    p.positions.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...["Symbol", "Budget", "Bought by", "Qty", "Avg cost", "Price", "Value", "Unrealized"].map((h, i) => el("th", { class: i > 2 ? "num" : "" }, h)))),
      el("tbody", {}, ...p.positions.map((x) => el("tr", {},
        el("td", {}, el("b", {}, x.symbol)), el("td", {}, budgetName(x.budgetId)), el("td", {}, ownerName(x.automationId)),
        el("td", { class: "num" }, fmt(x.quantity, 0)), el("td", { class: "num" }, money(x.averageCost)),
        el("td", { class: "num" }, money(x.price), el("span", { class: "muted" }, ["ALPACA", "IBKR"].includes(x.priceSource) ? " live" : " bar")),
        el("td", { class: "num" }, money(x.marketValue)),
        el("td", { class: `num ${tone(x.unrealizedPnL)}` }, signed(x.unrealizedPnL) + signedPct(x.unrealizedPct)))))) : el("p", { class: "muted" }, "No positions."),
    budgetsSection(p),
    orderForm(),
  );
}

function budgetsSection(p) {
  const form = { name: "", amount: 0 };
  const reload = async () => { await openPortfolio(p.id); };
  const call = (path, init) => guard(async () => { await api(path, init); await reload(); });
  const free = Object.values(pf.automations);
  const row = (b) => {
    const move = { amount: 100 };
    const attached = new Set(b.automations.map((a) => a.id));
    return el("div", { class: "note" },
      el("div", { class: "section-head" },
        el("b", {}, b.name),
        el("span", {}, el("button", { class: "link", onclick: () => guard(() => showBudgetLedger(b)) }, "Ledger"), " ",
          el("button", { class: "link danger", onclick: () => {
            if (confirm(`Delete budget "${b.name}"? Its cash goes back to the portfolio; it must hold no shares or open orders.`)) {
              call(`/v1/budgets/${b.id}`, { method: "DELETE" });
            }
          } }, "Delete"))),
      el("div", { class: "muted" }, `cash ${money(b.cash)} · available ${money(b.available)} · reserved ${money(b.reserved)} · invested ${money(b.invested)} · realized `,
        el("span", { class: tone(b.realizedPnL) }, signed(b.realizedPnL)), ` · fees ${money(b.fees)}`),
      el("div", { class: "row" }, "money",
        el("input", { type: "number", min: 0, step: "0.01", value: move.amount, style: "width:90px", oninput: (e) => (move.amount = Number(e.target.value)) }),
        el("button", { onclick: () => call(`/v1/budgets/${b.id}/allocate`, { method: "POST", body: { amount: move.amount } }) }, "Add from portfolio"),
        el("button", { onclick: () => call(`/v1/budgets/${b.id}/withdraw`, { method: "POST", body: { amount: move.amount } }) }, "Return to portfolio")),
      el("div", { class: "row" }, "automations",
        ...b.automations.map((a) => el("span", { class: "tag" }, a.name, " ",
          el("button", { class: "link", title: "Remove from this budget", onclick: () => call(`/v1/budgets/${b.id}/automations/${a.id}`, { method: "DELETE" }) }, "✕"))),
        b.automations.length ? null : el("span", { class: "muted" }, "none"),
        el("select", { onchange: (e) => e.target.value && call(`/v1/budgets/${b.id}/automations`, { method: "POST", body: { automationId: e.target.value } }) },
          el("option", { value: "" }, "+ add automation…"),
          ...free.filter((a) => !attached.has(a.id)).map((a) => el("option", { value: a.id }, a.name)))));
  };
  return el("div", {},
    el("h3", {}, "Budgets ", el("span", { class: "muted" }, "(automations in a budget share its money and shares)")),
    ...p.budgets.map(row),
    p.budgets.length ? null : el("p", { class: "muted" }, "No budgets yet."),
    el("div", { class: "row" }, "new budget",
      el("input", { type: "text", placeholder: "name", size: 14, oninput: (e) => (form.name = e.target.value) }),
      el("input", { type: "number", min: 0, step: "0.01", placeholder: "amount", style: "width:90px", oninput: (e) => (form.amount = Number(e.target.value) || 0) }),
      el("button", { class: "primary", onclick: () => call(`/v1/portfolios/${p.id}/budgets`, { method: "POST", body: form }) }, "Create"),
      el("span", { class: "muted" }, `unallocated: ${money(p.unallocatedCash)}`)));
}

async function showBudgetLedger(b) {
  const { entries } = await api(`/v1/budgets/${b.id}/ledger`);
  setChildren($("#results"), el("h2", {}, `Budget ledger · ${b.name}`),
    el("table", {},
      el("thead", {}, el("tr", {}, ...["Time", "Type", "Amount", "Cash after", "Reserved after", "Automation", "Note"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...entries.map((e) => el("tr", { title: [e.orderId, e.runId].filter(Boolean).join(" · ") },
        el("td", {}, when(e.createdAt)), el("td", {}, e.type.toLowerCase().replace("_", " ")),
        el("td", { class: `num ${tone(e.amount)}` }, signed(e.amount)), el("td", { class: "num" }, money(e.cashAfter)),
        el("td", { class: "num" }, money(e.reservedAfter)), el("td", {}, e.automationId ? ownerName(e.automationId) : ""),
        el("td", {}, e.note || ""))))),
    el("button", { class: "link", onclick: () => guard(showOrders) }, "Back to orders"));
}

function orderForm() {
  const o = { symbol: "", side: "BUY", type: "MARKET", quantity: 1, timeInForce: "DAY", limitPrice: null, budgetId: null };
  const sel = (key, options) => el("select", { onchange: (e) => { o[key] = e.target.value || null; } },
    ...options.map(([v, l]) => el("option", { value: v }, l)));
  const budgets = pf.data.budgets.map((b) => [b.id, `budget: ${b.name}`]);
  return el("div", {},
    el("h3", {}, "Manual order ", el("span", { class: "muted" }, "(paper account, halal symbols only)")),
    el("div", { class: "row" },
      sel("side", [["BUY", "Buy"], ["SELL", "Sell"]]),
      el("input", { type: "number", min: 1, step: 1, value: 1, style: "width:70px", oninput: (e) => (o.quantity = Number(e.target.value)) }),
      el("input", { type: "text", placeholder: "Symbol", size: 7, oninput: (e) => (o.symbol = e.target.value.toUpperCase()) }),
      sel("type", [["MARKET", "Market"], ["LIMIT", "Limit"]]),
      el("input", { type: "number", step: "0.01", placeholder: "limit", style: "width:80px", oninput: (e) => (o.limitPrice = e.target.value ? Number(e.target.value) : null) }),
      sel("timeInForce", [["DAY", "Day"], ["GTC", "GTC"]]),
      sel("budgetId", [["", "unallocated cash / manual shares"], ...budgets]),
      el("button", { class: "primary", onclick: () => guard(async () => {
        if (!confirm(`${o.side} ${o.quantity} ${o.symbol} (${o.type}) on the paper account?`)) return;
        const order = await api(`/v1/portfolios/${pf.data.id}/orders`, { method: "POST", body: o });
        await openPortfolio(pf.data.id);
        if (order.status === "REJECTED") setError(`Rejected by the broker: ${order.rejectReason}`);
      }) }, "Place")));
}

async function showOrders() {
  const [{ orders }, { fills }] = await Promise.all([
    api(`/v1/portfolios/${pf.selectedId}/orders?limit=100`), api(`/v1/portfolios/${pf.selectedId}/fills?limit=100`)]);
  const open_ = new Set(["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]);
  setChildren($("#results"), 
    el("h2", {}, `Orders · ${orders.length}`),
    orders.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...["Time", "Order", "Status", "Filled", "Avg", "Slip", "Owner", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...orders.map((o) => el("tr", { title: o.rejectReason || o.clientOrderId },
        el("td", {}, new Date(o.createdAt).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })),
        el("td", {}, `${o.side} ${fmt(o.quantity, 0)} ${o.symbol} ${o.type.toLowerCase()}${o.limitPrice ? ` @${o.limitPrice}` : ""}`),
        el("td", {}, el("span", { class: `badge ${o.status === "FILLED" ? "SUCCEEDED" : o.status === "REJECTED" ? "FAILED" : "SKIPPED"}` }, o.status.toLowerCase())),
        el("td", { class: "num" }, fmt(o.filledQuantity, 0)), el("td", { class: "num" }, money(o.averageFillPrice)),
        el("td", { class: `num ${tone(-(o.slippage || 0) * (o.side === "BUY" ? 1 : -1))}` }, o.slippage === null ? "—" : fmt(o.slippage, 3)),
        el("td", {}, o.budgetId ? `${budgetName(o.budgetId)} · ${ownerName(o.automationId)}` : ownerName(o.automationId)),
        el("td", {}, open_.has(o.status) ? el("button", { class: "link", onclick: () => guard(async () => {
          await api(`/v1/orders/${o.id}/cancel`, { method: "POST" });
          await openPortfolio(pf.selectedId);
        }) }, "cancel") : null))))) : el("p", { class: "muted" }, "No orders yet."),
    el("h2", { style: "margin-top:16px" }, `Fills · ${fills.length}`),
    fills.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...["Time", "Side", "Qty", "Symbol", "Price", "Fee"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...fills.map((f) => el("tr", {},
        el("td", {}, when(f.executedAt)), el("td", {}, f.side), el("td", { class: "num" }, fmt(f.quantity, 0)),
        el("td", {}, f.symbol), el("td", { class: "num" }, money(f.price)), el("td", { class: "num" }, money(f.fee)))))) : el("p", { class: "muted" }, "No fills yet."));
}

start("portfolio.html", loadAll);
