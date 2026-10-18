// Activity page: the app's log of every operation (API requests that change something, and what the app does
// by itself), with filters, paging, and everything that happened in the same request or run.

const act = { filter: { entityType: "", action: "", source: "", outcome: "", entityId: "" }, entries: [], next: null };
const TYPES = ["", "FILTER", "FILTER_GROUP", "AUTOMATION", "PORTFOLIO", "BROKER_ACCOUNT", "ORDER", "NOTIFICATION", "RUN"];

function query(beforeId) {
  const q = new URLSearchParams({ limit: 100 });
  for (const [k, v] of Object.entries(act.filter)) if (v) q.set(k, v);
  if (beforeId) q.set("beforeId", beforeId);
  return `/v1/activity?${q}`;
}

async function load(more = false) {
  const data = await api(query(more ? act.next : null));
  act.entries = more ? act.entries.concat(data.entries) : data.entries;
  act.next = data.nextBeforeId;
  $("#older").hidden = !act.next;
  renderTable();
}

async function renderFilters() {
  const { actions } = await api("/v1/activity/actions");
  const sel = (key, options, label) => el("select", { title: label, onchange: (e) => { act.filter[key] = e.target.value; load(); } },
    ...options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o || `any ${label}`]; return el("option", { value: v, selected: act.filter[key] === v }, l); }));
  setChildren($("#filters"),
    sel("entityType", TYPES, "type"),
    sel("action", ["", ...actions.map((a) => [a.action, `${a.action} (${a.count})`])], "action"),
    sel("source", ["", "API", "SCHEDULER", "SYNC", "SYSTEM"], "source"),
    sel("outcome", ["", "OK", "FAILED"], "outcome"),
    el("input", { type: "text", placeholder: "id (entity or related)", size: 26, value: act.filter.entityId,
      onchange: (e) => { act.filter.entityId = e.target.value.trim(); load(); } }),
    el("button", { class: "link", onclick: () => { act.filter = { entityType: "", action: "", source: "", outcome: "", entityId: "" }; renderFilters(); load(); } }, "clear"));
}

function renderTable() {
  setChildren($("#table-wrap"), act.entries.length ? el("table", {},
    el("thead", {}, el("tr", {}, ...["Time", "Source", "Action", "Entity", "Result"].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...act.entries.map((e) => el("tr", { onclick: () => show(e) },
      el("td", {}, new Date(e.at).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })),
      el("td", { class: "muted" }, e.source),
      el("td", {}, el("b", {}, e.action)),
      el("td", { class: "muted" }, e.entityType ? `${e.entityType.toLowerCase()} ${e.entityId ? e.entityId.slice(0, 12) + "…" : ""}` : ""),
      el("td", {}, el("span", { class: `badge ${e.outcome === "OK" ? "SUCCEEDED" : "FAILED"}` }, e.status ? `${e.status}` : e.outcome.toLowerCase()),
        e.error ? el("span", { class: "muted" }, ` ${e.error.slice(0, 60)}`) : null)))))
    : el("p", { class: "muted" }, "No entries match."));
}

async function show(e) {
  const linked = e.correlationId ? (await api(`/v1/activity?correlationId=${encodeURIComponent(e.correlationId)}&limit=200`)).entries : [];
  const idLink = (id) => el("button", { class: "link", onclick: () => { act.filter.entityId = id; renderFilters(); load(); } }, id);
  setChildren($("#results"),
    el("div", { class: "section-head" }, el("h2", {}, e.action), el("span", { class: `badge ${e.outcome === "OK" ? "SUCCEEDED" : "FAILED"}` }, e.outcome)),
    el("dl", { class: "kv" },
      el("dt", {}, "When"), el("dd", {}, when(e.at)),
      el("dt", {}, "Source"), el("dd", {}, `${e.source} · ${e.actor}`),
      e.method ? [el("dt", {}, "Request"), el("dd", {}, `${e.method} ${e.path} → ${e.status} (${e.durationMs} ms)`)] : null,
      e.entityId ? [el("dt", {}, e.entityType || "Entity"), el("dd", {}, idLink(e.entityId))] : null,
      ...Object.entries(e.related).map(([k, v]) => [el("dt", {}, k), el("dd", {}, idLink(v))]),
      e.error ? [el("dt", {}, "Error"), el("dd", { class: "down" }, e.error)] : null),
    el("h3", {}, "Details"),
    el("pre", { class: "json" }, JSON.stringify(e.details, null, 2)),
    linked.length > 1 ? [el("h3", {}, `Same request / run · ${linked.length}`),
      el("ul", {}, ...linked.reverse().map((x) => el("li", {}, el("button", { class: "link", onclick: () => show(x) }, x.action),
        ` ${x.entityType ? x.entityType.toLowerCase() : ""} ${x.outcome === "FAILED" ? "· failed" : ""}`)))] : null);
}

$("#refresh").onclick = () => guardLoad();
$("#older").onclick = () => load(true);

async function guardLoad() {
  try { await Promise.all([renderFilters(), load()]); } catch (e) { setChildren($("#table-wrap"), el("div", { class: "error" }, e.message)); }
}

start("activity.html", guardLoad);
