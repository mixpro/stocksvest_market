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

const st = { automations: [], filters: [], groups: [], selectedId: null, draft: null };

// ------------------------------------------------------------------ loading

async function loadAll() {
  const [a, f, g] = await Promise.all([api("/v1/automations"), api("/v1/filters"), api("/v1/filter-groups")]);
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
  $("#switch").replaceChildren(el("div", { class: `switch ${on ? "on" : "off"}` },
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
  $("#automation-list").replaceChildren(...st.automations.map((a) =>
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

function actionRow(a, i) {
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
  $("#editor").replaceChildren(
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
    el("h3", {}, "Actions ", el("span", { class: "muted" }, "(in order)")),
    ...d.actions.map(actionRow),
    select_([["", "+ Add action…"], ["NOTIFY", "Notify"], ["PLACE_ORDER", "Place order (phase 2)"], ["ADD_TO_WATCHLIST", "Add to watchlist (phase 3)"]], "",
      (v) => {
        if (v === "NOTIFY") d.actions.push({ type: "NOTIFY", title: "{name}: {count} matches", message: "", includeMatchedSymbols: true, skipWhenNoMatches: false });
        else if (v) d.actions.push({ type: v });  // the API explains these are not available yet
        rerender();
      }),
    el("div", { class: "toolbar" },
      el("button", { onclick: preview }, "Preview"),
      el("button", { onclick: nextRuns }, "Next runs"),
      el("button", { class: "primary", onclick: save }, saved ? "Save" : "Create"),
      el("button", { onclick: runNow, disabled: !saved }, "Run now"),
      el("button", { onclick: togglePause, disabled: !saved }, paused ? "Resume" : "Pause"),
      el("button", { onclick: history, disabled: !saved }, "History"),
      el("button", { onclick: changes, disabled: !saved }, "Changes"),
      el("button", { class: "danger", onclick: remove, disabled: !saved }, "Delete")),
  );
}

function body() {
  const d = st.draft;
  return { name: d.name, description: d.description, runTargets: d.runTargets, schedules: d.schedules, actions: d.actions };
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
  if (!confirm(`Delete "${st.draft.name}"? Its run history is kept.`)) return;
  await api(`/v1/automations/${st.selectedId}`, { method: "DELETE" });
  st.selectedId = null; st.draft = null;
  $("#editor").replaceChildren(el("p", { class: "muted empty" }, "Deleted."));
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

const changes = () => guard(async () => {
  const { events } = await api(`/v1/automations/${st.selectedId}/events`);
  showResults(el("h2", {}, "Changes"), ...events.map((e) => el("div", { class: "note" },
    el("b", {}, e.type.toLowerCase()), ` · v${e.configVersion} · ${when(e.createdAt)}`,
    Object.keys(e.changes).length && e.type === "UPDATED" ? el("div", { class: "muted" }, `changed: ${Object.keys(e.changes).join(", ")}`) : null)));
});

// ------------------------------------------------------------------ results

function showResults(...nodes) {
  $("#results").replaceChildren(...nodes.filter(Boolean));
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
