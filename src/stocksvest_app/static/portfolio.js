// Portfolio page: broker accounts, portfolios, positions by owner, budgets, manual orders, orders and fills.

const pf = { accounts: [], portfolios: [], automations: {}, selectedId: null, data: null };

function setError(message) {
  $("#editor .error")?.remove();
  if (message) $("#editor").prepend(el("div", { class: "error" }, message));
}

async function guard(fn) {
  setError(null);
  try { await fn(); } catch (e) { setError(e.message); }
}

const ownerName = (id) => (id ? pf.automations[id]?.name || id : "manual");

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
    el("li", { class: "stacked", title: x.lastError || "" },
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

$("#new-portfolio").onclick = () => {
  const free = pf.accounts.filter((a) => !pf.portfolios.some((p) => p.brokerAccountId === a.id));
  const form = { name: "Paper portfolio", description: "", brokerAccountId: free[0]?.id };
  setChildren($("#editor"), 
    el("h2", {}, "New portfolio"),
    free.length ? null : el("p", { class: "error" }, "Every broker account already has a portfolio. Add a broker account first."),
    el("p", { class: "muted" }, "Shares already in the account become manual (opening) positions."),
    el("label", { class: "field" }, el("span", {}, "Name"), el("input", { type: "text", value: form.name, oninput: (e) => (form.name = e.target.value) })),
    el("label", { class: "field" }, el("span", {}, "Broker account"), el("select", { onchange: (e) => (form.brokerAccountId = e.target.value) },
      ...free.map((a) => el("option", { value: a.id }, a.name)))),
    el("div", { class: "toolbar" }, el("button", { class: "primary", disabled: !free.length, onclick: () => guard(async () => {
      const p = await api("/v1/portfolios", { method: "POST", body: form });
      pf.selectedId = p.id;
      await loadAll();
      await openPortfolio(p.id);
    }) }, "Create")));
};

// ------------------------------------------------------------------ portfolio view

async function openPortfolio(id) {
  pf.selectedId = id;
  pf.data = await api(`/v1/portfolios/${id}`);
  render();
  await showOrders();
}

function render() {
  const p = pf.data;
  const sync = p.lastSync;
  const totals = p.positions.reduce((t, x) => ({ value: t.value + (x.marketValue || 0), pnl: t.pnl + (x.unrealizedPnL || 0) }), { value: 0, pnl: 0 });
  setChildren($("#editor"), 
    el("div", { class: "section-head" }, el("h2", {}, p.name),
      el("button", { onclick: () => guard(async () => { const r = await api(`/v1/portfolios/${p.id}/sync`, { method: "POST" }); pf.data = r.portfolio; render(); showOrders(); }) }, "Sync now")),
    el("dl", { class: "kv" },
      el("dt", {}, "Cash"), el("dd", {}, `${money(p.cash)} ${p.currency}`),
      el("dt", {}, "Unallocated"), el("dd", {}, money(p.unallocatedCash)),
      el("dt", {}, "Equity"), el("dd", {}, money(p.equity)),
      el("dt", {}, "Positions"), el("dd", {}, `${money(totals.value)} · unrealized `, el("span", { class: tone(totals.pnl) }, signed(totals.pnl))),
      el("dt", {}, "Realized P&L"), el("dd", { class: tone(p.realizedPnL) }, signed(p.realizedPnL)),
      el("dt", {}, "Last sync"), el("dd", {}, sync ? `${when(sync.ranAt)}${sync.error ? ` · ${sync.error}` : ""}` : "—")),
    sync?.mismatches?.length ? el("div", { class: "error" }, "Differences with the broker:\n" +
      sync.mismatches.map((m) => `${m.kind} ${m.symbol || ""} app ${m.appValue ?? ""} broker ${m.brokerValue ?? ""} ${m.detail || ""}`).join("\n")) : null,
    el("h3", {}, "Positions"),
    p.positions.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...["Symbol", "Owner", "Qty", "Avg cost", "Price", "Value", "Unrealized"].map((h, i) => el("th", { class: i > 1 ? "num" : "" }, h)))),
      el("tbody", {}, ...p.positions.map((x) => el("tr", {},
        el("td", {}, el("b", {}, x.symbol)), el("td", {}, ownerName(x.automationId)),
        el("td", { class: "num" }, fmt(x.quantity, 0)), el("td", { class: "num" }, money(x.averageCost)),
        el("td", { class: "num" }, money(x.price)), el("td", { class: "num" }, money(x.marketValue)),
        el("td", { class: `num ${tone(x.unrealizedPnL)}` }, signed(x.unrealizedPnL)))))) : el("p", { class: "muted" }, "No positions."),
    el("h3", {}, "Automation budgets"),
    p.budgets.length ? el("table", {},
      el("thead", {}, el("tr", {}, ...["Automation", "Cash", "Reserved", "Available", "Invested", "Realized"].map((h, i) => el("th", { class: i ? "num" : "" }, h)))),
      el("tbody", {}, ...p.budgets.map((b) => el("tr", {},
        el("td", {}, ownerName(b.automationId)), el("td", { class: "num" }, money(b.cash)), el("td", { class: "num" }, money(b.reserved)),
        el("td", { class: "num" }, money(b.available)), el("td", { class: "num" }, money(b.invested)),
        el("td", { class: `num ${tone(b.realizedPnL)}` }, signed(b.realizedPnL)))))) : el("p", { class: "muted" }, "No budgets yet: allocate money on the Automations page."),
    orderForm(),
  );
}

function orderForm() {
  const o = { symbol: "", side: "BUY", type: "MARKET", quantity: 1, timeInForce: "DAY", limitPrice: null, automationId: null };
  const sel = (key, options) => el("select", { onchange: (e) => { o[key] = e.target.value || null; } },
    ...options.map(([v, l]) => el("option", { value: v }, l)));
  const budgets = pf.data.budgets.map((b) => [b.automationId, `for ${ownerName(b.automationId)}`]);
  return el("div", {},
    el("h3", {}, "Manual order ", el("span", { class: "muted" }, "(paper account, halal symbols only)")),
    el("div", { class: "row" },
      sel("side", [["BUY", "Buy"], ["SELL", "Sell"]]),
      el("input", { type: "number", min: 1, step: 1, value: 1, style: "width:70px", oninput: (e) => (o.quantity = Number(e.target.value)) }),
      el("input", { type: "text", placeholder: "Symbol", size: 7, oninput: (e) => (o.symbol = e.target.value.toUpperCase()) }),
      sel("type", [["MARKET", "Market"], ["LIMIT", "Limit"]]),
      el("input", { type: "number", step: "0.01", placeholder: "limit", style: "width:80px", oninput: (e) => (o.limitPrice = e.target.value ? Number(e.target.value) : null) }),
      sel("timeInForce", [["DAY", "Day"], ["GTC", "GTC"]]),
      sel("automationId", [["", "manual"], ...budgets]),
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
        el("td", {}, ownerName(o.automationId)),
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
