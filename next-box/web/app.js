const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const RANK = { user: 0, admin: 1, superadmin: 2 };
let me = null;

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith("/auth/")) { showLogin("Your session ended. Sign in again."); }
  if (!r.ok) throw new Error(body.detail || `${r.status}`);
  return body;
}
const png = (id) => `/printed/${id}/png?t=${Date.now()}`;
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "never";

let toastTimer;
function toast(msg) {
  $("#toast").textContent = msg;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 1800);
}

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("theme") || ""); $("#theme").value = localStorage.getItem("theme") || ""; } catch {}
$("#theme").onchange = () => { applyTheme($("#theme").value); try { localStorage.setItem("theme", $("#theme").value); } catch {} };

// ---------------------------------------------------------------- sign in
function showOnly(id) {
  for (const x of ["login", "mustChange", "appShell"]) $("#" + x).hidden = x !== id;
}
function showLogin(msg = "") {
  me = null;
  showOnly("login");
  $("#loginMsg").textContent = msg;
  $("#user").focus();
}

async function boot() {
  try { me = await api("/auth/me"); }
  catch (e) { return showLogin(e.message === "sign in required" ? "" : e.message); }
  if (me.must_change) { showOnly("mustChange"); $("#mcCur").focus(); return; }
  showOnly("appShell");
  $("#who").textContent = me.username;
  for (const el of [$("#roleBadge"), $("#whoRole")]) {
    el.textContent = me.role;
    el.classList.toggle("hot", me.role !== "user");
  }
  $("#roleBadge").hidden = me.role === "user";
  $("#betaBadge").hidden = !me.beta;
  $("#announce").hidden = !me.announcement;
  $("#announce").textContent = me.announcement || "";
  $("#paused").hidden = !me.printing_paused;
  $$("[data-min]").forEach((el) => el.hidden = RANK[me.role] < RANK[el.dataset.min]);
  $$("#nuRole option[data-min]").forEach((o) => o.disabled = RANK[me.role] < RANK[o.dataset.min]);
  loadPrefs(me.prefs);
  refreshStatus();
  loadConnection();
  loadLabels();
  $("#noticeText").textContent = me.data_notice;
  $("#noticeWhen").textContent = me.consented ? `You accepted this on ${when(me.consented_at)}.` : "";
  if (!me.consented) { $("#noticeBody").textContent = me.data_notice; $("#noticeDialog").showModal(); }
}

$("#noticeOk").onclick = async () => {
  try { await api("/auth/consent", { method: "POST" }); $("#noticeDialog").close(); boot(); }
  catch (e) { toast(e.message); }
};
$("#noticeDialog").addEventListener("cancel", (e) => e.preventDefault());  // must be acknowledged

// ---------------------------------------------------------------- revision mark
api("/version").then((v) => {
  $("#revmark").textContent = ["Next Box rev " + v.version, v.build, v.credit].filter(Boolean).join(" · ");
}).catch(() => {});

// ---------------------------------------------------------------- printers & label colors
const SWATCH = { white: "#fff", red: "#e53935", orange: "#fb8c00", yellow: "#fdd835", green: "#43a047", blue: "#1e88e5",
                 pink: "#ec407a", purple: "#8e24aa", clear: "transparent", other: "#9e9e9e" };
const sw = (c) => `<span class="swatch" style="background:${SWATCH[c] || "#ccc"}"></span>`;
let printOpts = null;

async function loadLabels() {
  try { printOpts = await api("/printing-options"); } catch (e) { $("#printersLoaded").textContent = e.message; return; }
  const ps = printOpts.printers;
  $("#printersLoaded").innerHTML = ps.length
    ? "Loaded right now: " + ps.map((p) => `${sw(p.stock_color)}${esc(p.name)} (${esc(p.stock_color)} labels${p.ink === "black_red" ? ", red ink" : ""})`).join(", ")
    : "No printers set up yet. An admin can add them under Admin → Printers.";
  $("#defPrinter").innerHTML = `<option value="">First printer</option>` + ps.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#defPrinter").value = me.prefs.default_printer || "";
  const rules = me.prefs.color_rules || {};
  $("#colorRules").innerHTML = Object.entries(printOpts.reasons).map(([k, label]) => `
    <label class="setting"><div class="label"><b>${esc(label)}</b>
      <span>${rules[k] && rules[k] !== "any" ? (ps.some((p) => p.stock_color === rules[k]) ? "" : `No printer has ${esc(rules[k])} labels loaded right now.`) : ""}</span></div>
      <select data-rule="${esc(k)}" aria-label="${esc(label)} label color">
        <option value="any">usual printer</option>${printOpts.colors.map((c) => `<option value="${c}" ${rules[k] === c ? "selected" : ""}>${c} labels</option>`).join("")}
      </select></label>`).join("");
  $$("#colorRules select").forEach((sel) => sel.onchange = async () => {
    const next = {}; $$("#colorRules select").forEach((x) => next[x.dataset.rule] = x.value);
    try { me.prefs = await api("/settings", { method: "PATCH", json: { color_rules: next } }); toast("Saved"); loadLabels(); }
    catch (e) { toast(e.message); }
  });
}
$("#defPrinter").onchange = async () => {
  try { me.prefs = await api("/settings", { method: "PATCH", json: { default_printer: $("#defPrinter").value } }); toast("Saved"); }
  catch (e) { toast(e.message); }
};

async function loadPrinters() {
  try {
    printOpts = await api("/printing-options");
    const status = await api("/admin/printers/status").catch(() => ({}));
    const ps = printOpts.printers;
    $("#printerList").innerHTML = ps.map((p) => `<div class="user" data-pid="${esc(p.id)}"><div class="top">
        <span class="dot ${status[p.id] ? "on" : "off"}" title="${status[p.id] ? "reachable" : "not answering"}"></span>
        <b>${esc(p.name)}</b> <span class="muted">${esc(printOpts.drivers[p.driver]?.name || p.driver)}</span></div>
      <div class="muted">${esc(p.address)} · ${p.width_px} dots${p.ink === "black_red" ? " · black + red roll" : ""}</div>
      <div class="row">${sw(p.stock_color)}<select class="small" data-p="color" aria-label="Label color">${printOpts.colors.map((c) => `<option ${c === p.stock_color ? "selected" : ""}>${c}</option>`).join("")}</select>
        <button class="plain small" data-p="preview">Test (preview)</button><button class="plain small" data-p="print">Test print</button>
        <button class="plain small" data-p="remove">Remove</button></div></div>`).join("") || "No printers yet. Add one below.";
    if (!$("#npDriver").options.length) {
      $("#npDriver").innerHTML = Object.entries(printOpts.drivers).map(([k, d]) => `<option value="${k}">${esc(d.name)}</option>`).join("");
      $("#npColor").innerHTML = printOpts.colors.map((c) => `<option>${c}</option>`).join("");
      $("#npDriver").onchange = driverHints; driverHints();
    }
  } catch (e) { $("#printerList").textContent = e.message; }
}
function driverHints() {
  const d = printOpts.drivers[$("#npDriver").value];
  $("#npHint").textContent = "e.g. " + d.address; $("#npWidth").value = d.width;
  $("#npInkRow").hidden = $("#npDriver").value !== "brother_ql";
  $("#npHeightRow").hidden = !["zpl", "tspl"].includes($("#npDriver").value);
}
$("#npGo").onclick = async () => {
  $("#npMsg").textContent = "";
  try {
    await api("/admin/printers", { method: "POST", json: { driver: $("#npDriver").value, name: $("#npName").value,
      address: $("#npAddress").value, stock_color: $("#npColor").value, ink: $("#npInk").checked ? "black_red" : "black",
      width_px: Number($("#npWidth").value) || null, label_height_mm: Number($("#npHeight").value) || 0 } });
    $("#npName").value = $("#npAddress").value = ""; toast("Printer added"); loadPrinters(); loadLabels();
  } catch (e) { $("#npMsg").textContent = e.message; }
};
$("#printerList").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-p]"); if (!b) return;
  const pid = b.closest("[data-pid]").dataset.pid;
  try {
    if (b.dataset.p === "remove") { if (!confirm("Remove this printer?")) return; await api(`/admin/printers/${pid}`, { method: "DELETE" }); }
    else {
      const real = b.dataset.p === "print";
      if (real && !confirm("Send a real test print?")) return;
      const r = await api(`/admin/printers/${pid}/test?dry_run=${!real}`, { method: "POST" });
      $("#nuOut").innerHTML = `<p>${r.status === "printed" ? "Printed" : "Preview"} on ${esc(r.printer.name)}:</p><img src="${png(r.id)}" style="max-width:100%;border:1px solid var(--line);border-radius:6px">`;
    }
    loadPrinters(); loadLabels();
  } catch (err) { toast(err.message); }
});
$("#printerList").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-p=color]"); if (!sel) return;
  try { await api(`/admin/printers/${sel.closest("[data-pid]").dataset.pid}`, { method: "PATCH", json: { stock_color: sel.value } }); toast("Saved"); loadPrinters(); loadLabels(); }
  catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- usage (superadmin)
const REASON_NAMES = { overdue: "overdue", urgent: "urgent", today: "daily", list: "lists", task: "single", note: "notes", new_task: "new tasks", text: "notes" };
async function loadUsage() {
  try {
    const u = await api(`/super/usage?days=${$("#uDays").value}`);
    const t = u.totals;
    $("#uTotals").innerHTML = [["People", `${t.people} (${t.active} active)`], ["Tickets", `${t.tickets} (${t.printed} printed, ${t.tickets - t.printed} previews)`], ["Read-backs", t.scans]]
      .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const max = Math.max(1, ...u.series.map((d) => d.tickets + d.scans));
    $("#uChart").innerHTML = u.series.map((d) => `<div style="height:${Math.round((d.tickets + d.scans) / max * 100)}%" title="${d.date}: ${d.tickets} tickets, ${d.scans} scans, ${d.people} people"></div>`).join("");
    $("#uPeople").innerHTML = u.people.map((p) => `<div class="card"><div class="top" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <b>${esc(p.username)}</b><span class="badge ${p.role !== "user" ? "hot" : ""}">${esc(p.role)}</span>${p.beta ? `<span class="badge">beta</span>` : ""}
        <span class="muted" style="margin-left:auto">active ${when(p.last_active)}</span></div>
      <div class="muted">${p.app ? esc(p.app) : "no to-do app"} · ${p.consented_at ? "notice accepted " + when(p.consented_at) : "<span class='err'>notice not accepted yet</span>"}</div>
      <div style="margin-top:6px">${p.tickets} tickets (${p.printed} printed) · ${p.scans} read-backs · marks: ${p.marks_applied} auto, ${p.marks_confirmed} confirmed, ${p.marks_skipped} skipped</div>
      <div class="muted">${Object.entries(p.by_reason).map(([k, v]) => `${esc(REASON_NAMES[k] || k)} ${v}`).join(" · ") || "no tickets yet"}</div>
      <div class="muted">most used: ${p.top_actions.map(([a, n]) => `${esc(a)} ×${n}`).join(", ") || "nothing yet"}</div>
      <div class="muted">settings: ${p.prefs.max_rows} rows${p.prefs.split_overdue ? ", overdue split" : ""}${p.prefs.always_dry_run ? ", preview only" : ""}, labels: ${Object.entries(p.prefs.color_rules || {}).map(([k, c]) => `${esc(REASON_NAMES[k] || k)}→${esc(c)}`).join(", ") || "default"}</div>
      <div class="row"><button class="plain small" data-u="${esc(p.username)}">View tickets</button></div></div>`).join("");
  } catch (e) { $("#uPeople").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
$("#uDays").onchange = loadUsage;
$("#uPeople").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-u]"); if (!b) return;
  const d = await api(`/super/users/${encodeURIComponent(b.dataset.u)}/tickets?limit=24`);
  $("#uWho").textContent = b.dataset.u; $("#uDetail").hidden = false;
  $("#uTickets").innerHTML = `<div class="tickets">${d.tickets.map((r) => `<div><img loading="lazy" src="${png(r.id)}" alt="">
      <div class="muted">${esc(r.created_at.replace("T", " "))}${r.printer ? " · " + esc(r.printer.name) : ""}${r.dry_run ? " · preview" : ""}</div></div>`).join("") || "<p class='muted'>No tickets.</p>"}</div>
    <h3>Read-backs</h3>${d.scans.map((r) => `<div class="muted">${esc(r.created_at.replace("T", " "))}: ${r.applied.length} applied, ${r.needs_confirmation.length} needed a tap${r.errors.length ? ", " + r.errors.length + " problems" : ""}</div>`).join("") || "<p class='muted'>None.</p>"}`;
  $("#uDetail").scrollIntoView({ behavior: "smooth" });
});

// ---------------------------------------------------------------- to-do app connection
let providers = [], conn = null;

async function loadConnection() {
  try {
    [providers, conn] = await Promise.all([providers.length ? providers : api("/providers"), api("/connection")]);
  } catch (e) { $("#appStatus").textContent = e.message; return; }
  const ready = providers.filter((p) => p.status === "ready");
  const spec = providers.find((p) => p.key === conn.provider);
  $("#notConnected").hidden = !!conn.connected;
  $$(".appName").forEach((el) => el.textContent = conn.connected ? conn.name : "your to-do app");
  $("#filterHelp").textContent = spec?.filter_help || "";
  if (conn.connected) {
    $("#appStatus").innerHTML = `Connected to <b>${esc(conn.name)}</b>${conn.account ? ` as ${esc(conn.account)}` : ""}.
      ${conn.legacy ? `<div class="muted">Using the Todoist token in the server's .env. Connect here to use your own.</div>` : ""}
      ${(conn.lists || []).length ? `<div class="muted">Lists: ${conn.lists.map(esc).join(", ")}</div>` : ""}
      ${conn.legacy ? "" : `<div class="row"><button class="plain small" id="appDisconnect">Disconnect</button>
        <button class="plain small" id="appChange">Switch app</button></div>`}`;
    $("#appConnect").hidden = !conn.legacy;
    $("#appDisconnect")?.addEventListener("click", async () => {
      if (!confirm("Disconnect your to-do app? Your tasks stay in the app; Next Box just stops reading them.")) return;
      await api("/connection", { method: "DELETE" }); toast("Disconnected"); loadConnection();
    });
    $("#appChange")?.addEventListener("click", () => { $("#appConnect").hidden = false; });
  } else {
    $("#appStatus").textContent = "Not connected yet. Pick your app below.";
    $("#appConnect").hidden = false;
  }
  if (!$("#provSel").options.length) {
    $("#provSel").innerHTML = ready.map((p) => `<option value="${esc(p.key)}">${esc(p.name)}</option>`).join("");
    $("#provSel").onchange = renderProviderFields;
    $("#provOther").innerHTML = (ready.filter((p) => p.also).map((p) => `<p><b>${esc(p.name)}</b> also covers ${esc(p.also)}.</p>`).join("")) +
      providers.filter((p) => p.status !== "ready").map((p) => `<p><b>${esc(p.name)}</b>: ${p.status === "needs_setup" ? "coming soon." : "not possible."} ${esc(p.reason)}</p>`).join("");
    renderProviderFields();
  }
}

function renderProviderFields() {
  const p = providers.find((x) => x.key === $("#provSel").value);
  $("#provFields").innerHTML = (p.fields || []).map((f) => `<div class="field">
      <label for="pf_${esc(f.name)}">${esc(f.label)}${f.optional ? " (optional)" : ""}</label>
      <input id="pf_${esc(f.name)}" data-field="${esc(f.name)}" type="${f.secret ? "password" : "text"}" autocomplete="off" autocapitalize="none">
      ${f.help ? `<div class="muted">${esc(f.help)}</div>` : ""}</div>`).join("");
  $("#provMsg").textContent = "";
}

$("#provGo").onclick = async () => {
  const fields = {};
  $$("#provFields [data-field]").forEach((el) => fields[el.dataset.field] = el.value);
  $("#provGo").disabled = true; $("#provMsg").textContent = "";
  try {
    await api("/connection", { method: "PUT", json: { provider: $("#provSel").value, fields } });
    $$("#provFields input").forEach((el) => el.value = "");
    toast("Connected"); loadConnection();
  } catch (e) { $("#provMsg").textContent = e.message; }
  finally { $("#provGo").disabled = false; }
};
$("#goConnect").onclick = () => { $("nav button[data-tab=settings]").click(); $("#appCard").scrollIntoView({ behavior: "smooth" }); };

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/login", { method: "POST", json: { username: $("#user").value, password: $("#pass").value } });
    $("#pass").value = "";
    boot();
  } catch (err) { $("#loginMsg").textContent = err.message; }
};
$("#mustForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/password", { method: "POST", json: { current: $("#mcCur").value, new: $("#mcNew").value } });
    $("#mcCur").value = $("#mcNew").value = "";
    toast("Password saved");
    boot();
  } catch (err) { $("#mcMsg").textContent = err.message; }
};
$("#logout").onclick = async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); showLogin("Signed out."); };

// ---------------------------------------------------------------- tabs
const loaders = { printed: loadHistory, settings: () => { loadSessions(); loadLabels(); }, admin: () => { loadAdmin(); loadPrinters(); },
                  debug: loadDebug, board: () => loadBoard(), usage: () => loadUsage() };
$$("nav button").forEach((b) => b.onclick = () => {
  $$("nav button").forEach((x) => x.classList.toggle("on", x === b));
  $$("section").forEach((s) => s.classList.toggle("on", s.id === b.dataset.tab));
  loaders[b.dataset.tab]?.();
});

async function refreshStatus() {
  try {
    const s = await api("/status");
    $("#status").textContent = `${s.printer || "printer"} ${s.printer_reachable ? "online" : "offline"}${s.dry_run_forced ? " · preview only" : ""}`;
  } catch (e) { $("#status").textContent = `server: ${e.message}`; }
}

// ---------------------------------------------------------------- print
function showPrint(r) {
  const box = $("#printResult");
  box.hidden = false;
  if (r.status === "skipped") { box.innerHTML = `<b>Skipped</b> ${esc(r.reason)}`; return; }
  const one = (t) => `<p><b>${t.status === "printed" ? "Printed" : "Preview"}</b> #${esc(t.id)} · ${t.manifest.length} rows
    ${t.printer ? ` · ${sw(t.printer.color)}${esc(t.printer.name)}` : ""}</p>${t.note ? `<p class="muted">${esc(t.note)}</p>` : ""}
    <img src="${png(t.id)}" alt="ticket preview">`;
  box.innerHTML = [...(r.also || []), r].map(one).join("<hr>");
}

$$("[data-act]").forEach((b) => b.onclick = async () => {
  const dry_run = $("#dry").checked;
  const act = b.dataset.act;
  const routes = {
    today: ["/print/today", { source: "manual", dry_run }],
    list: ["/print/list", { query: $("#query").value, dry_run }],
    text: ["/print/text", { text: $("#text").value, dry_run }],
    todo: ["/print/text", { text: $("#text").value, todo: true, dry_run }],
    task: ["/print/task", { task_id: $("#taskid").value, dry_run }],
  };
  if (!dry_run && !confirm("Send this to the printer?")) return;
  b.disabled = true;
  try { showPrint(await api(routes[act][0], { method: "POST", json: routes[act][1] })); }
  catch (e) {
    $("#printResult").hidden = false; $("#printResult").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    if (e.message.startsWith("Connect your to-do app")) loadConnection();
  }
  finally { b.disabled = false; }
});

// ---------------------------------------------------------------- read back
const VERB = { drop: "Delete", done: "Complete", tomorrow: "Move to tomorrow" };

function renderScan(rec) {
  const pend = rec.needs_confirmation.filter((p) => p.status === "pending");
  let h = `<div class="card"><h2>Scan ${esc(rec.id)}${rec.label_id ? ` · ticket #${esc(rec.label_id)}` : ""}</h2>`;
  rec.errors.forEach((e) => h += `<p class="err">${esc(e)}</p>`);
  rec.applied.forEach((a) => h += `<p class="applied">✓ ${esc(VERB[a.mark])}: ${esc(a.content)}</p>`);
  rec.needs_confirmation.filter((p) => p.status !== "pending")
    .forEach((p) => h += `<p class="muted">${esc(p.status)}: ${esc(VERB[p.mark])} ${esc(p.content)}</p>`);
  pend.forEach((p) => h += `<div class="pending"><b>${esc(VERB[p.mark])}?</b> ${esc(p.content)}
      <div class="muted">row ${p.row} · confidence ${p.confidence}${p.note ? " · " + esc(p.note) : ""}</div>
      <div class="row"><button class="go ${p.mark === "drop" ? "warn" : ""}" data-row="${p.row}" data-d="confirm">Confirm</button>
      <button class="plain" data-row="${p.row}" data-d="skip">Skip</button></div></div>`);
  if (!rec.applied.length && !rec.needs_confirmation.length && !rec.errors.length) h += `<p class="muted">No marks found.</p>`;
  $("#scanResult").innerHTML = h + "</div>";
  $("#scanResult").querySelectorAll("[data-row]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try {
      renderScan(await api(`/scan/${rec.id}/confirm`, { method: "POST", json: { decisions: { [b.dataset.row]: b.dataset.d } } }));
    } catch (e) { alert(e.message); b.disabled = false; }
  });
}

$("#scanGo").onclick = async () => {
  const f = $("#photo").files[0];
  if (!f) return alert("Pick a photo first");
  const fd = new FormData();
  fd.append("photo", f);
  if ($("#scanLabel").value.trim()) fd.append("label_id", $("#scanLabel").value.trim().replace(/^#/, ""));
  $("#scanGo").disabled = true;
  $("#scanResult").innerHTML = `<p class="muted">Reading…</p>`;
  try { renderScan(await api("/scan", { method: "POST", body: fd })); }
  catch (e) { $("#scanResult").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  finally { $("#scanGo").disabled = false; }
};

// ---------------------------------------------------------------- history
async function loadHistory() {
  try {
    const items = await api("/printed?limit=30");
    $("#hist").innerHTML = items.length ? items.map((r) => `<div class="card">
      <div class="item"><b>${esc(r.title).slice(0, 50)}</b><span class="muted">#${esc(r.id)}</span></div>
      <div class="muted">${esc(r.created_at.replace("T", " "))} · ${esc(r.kind)} · ${esc(r.by || r.source)}${r.dry_run ? " · preview" : ""}</div>
      <img loading="lazy" src="${png(r.id)}" alt=""></div>`).join("") : `<p class="muted">Nothing printed yet.</p>`;
  } catch (e) { $("#hist").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

// ---------------------------------------------------------------- settings
function confText(v) { $("#confLabel").textContent = `Marks below ${Math.round(v * 100)}% wait for you.`; }

function loadPrefs(prefs) {
  $$("[data-pref]").forEach((el) => {
    const v = prefs[el.dataset.pref];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  confText(prefs.confidence);
  $("#dry").checked = true;
  $("#dry").disabled = !!prefs.always_dry_run;
  if (prefs.default_filter && !$("#query").value) $("#query").value = prefs.default_filter;
}

$$("[data-pref]").forEach((el) => el.onchange = async () => {
  const k = el.dataset.pref;
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number") v = parseInt(v, 10);
  if (el.type === "range") v = parseFloat(v);
  try {
    const prefs = await api("/settings", { method: "PATCH", json: { [k]: v } });
    me.prefs = prefs;
    loadPrefs(prefs);
    toast("Saved");
  } catch (e) { toast(e.message); loadPrefs(me.prefs); }
});
$("[data-pref=confidence]").oninput = (e) => confText(e.target.value);

$("#pwForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/password", { method: "POST", json: { current: $("#pwCur").value, new: $("#pwNew").value } });
    $("#pwCur").value = $("#pwNew").value = "";
    $("#pwMsg").textContent = "Changed. Your other devices were signed out.";
    loadSessions();
  } catch (err) { $("#pwMsg").textContent = err.message; }
};

async function loadSessions() {
  try {
    const list = await api("/auth/sessions");
    $("#sessions").innerHTML = list.map((s) => `<div>${s.current ? "<b>This device</b>" : esc(s.agent.split(")")[0].split("(")[1] || s.agent || "Unknown device")}
      · ${esc(s.ip)} · since ${when(s.created)}</div>`).join("") || "None";
  } catch (e) { $("#sessions").textContent = e.message; }
}
$("#signoutOthers").onclick = async () => {
  const r = await api("/auth/signout-others", { method: "POST" });
  toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? "" : "s"}`);
  loadSessions();
};

// ---------------------------------------------------------------- feedback (anonymous)
let fbMode = "guided";
const currentPage = () => (me ? document.querySelector("nav button.on")?.dataset.tab : "login") || "";

function pick(group, el) { $$(`${group} button`).forEach((b) => b.classList.toggle("on", b === el)); }
$$("#fbType button").forEach((b) => b.onclick = () => pick("#fbType", b));
$$("#fbRating button").forEach((b) => b.onclick = () => b.classList.contains("on") ? b.classList.remove("on") : pick("#fbRating", b));
$$(".seg button").forEach((b) => b.onclick = () => {
  fbMode = b.dataset.mode; pick(".seg", b);
  $("#fbGuided").hidden = fbMode !== "guided"; $("#fbOpen").hidden = fbMode !== "open";
});

function openFeedback() {
  $("#fbErr").textContent = ""; $("#fbDone").hidden = true; $("#fbSend").hidden = false;
  $("#fbForm").querySelectorAll("textarea").forEach((t) => t.value = "");
  $$("#fbRating button").forEach((b) => b.classList.remove("on"));
  $("#fbDebugRow").hidden = !me;  // error details exist only for signed-in people
  $("#fbDebug").checked = false;
  $("#fbDialog").showModal();
}
document.addEventListener("click", (e) => { if (e.target.closest("[data-feedback]")) { e.preventDefault(); openFeedback(); } });
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) $(`nav button[data-tab=${g.dataset.goto}]`).click(); });
$("#fbClose").onclick = () => $("#fbDialog").close();

$("#fbSend").onclick = async () => {
  const body = { mode: fbMode, page: currentPage(), include_debug: $("#fbDebug").checked };
  if (fbMode === "guided") {
    Object.assign(body, { type: $("#fbType button.on")?.dataset.v || "other",
      rating: $("#fbRating button.on") ? Number($("#fbRating button.on").dataset.v) : null,
      trying: $("#fbTrying").value, happened: $("#fbHappened").value, expected: $("#fbExpected").value });
  } else {
    Object.assign(body, { type: "other", message: $("#fbMessage").value });
  }
  $("#fbSend").disabled = true; $("#fbErr").textContent = "";
  try {
    const item = await api("/feedback", { method: "POST", json: body });
    $("#fbDone").innerHTML = `<p class="ok"><b>Posted anonymously. Thank you!</b> Here's exactly what everyone will see:</p>${feedbackCard(item, false)}`;
    $("#fbDone").hidden = false; $("#fbSend").hidden = true;
    if (document.querySelector("nav button.on")?.dataset.tab === "board") loadBoard();
  } catch (e) { $("#fbErr").textContent = e.message; }
  finally { $("#fbSend").disabled = false; }
};

const KIND = { bug: "Something broke", confusing: "Confusing", idea: "Idea", praise: "Love it", other: "Other" };
const STATUS = { new: "new", seen: "seen", planned: "planned", fixed: "fixed", wontfix: "won't fix", hidden: "hidden" };

function feedbackCard(f, controls = true) {
  const admin = controls && me && RANK[me.role] >= RANK.admin;
  const q = (label, v) => v ? `<div class="q">${label}</div><div>${esc(v)}</div>` : "";
  return `<div class="card fbitem" data-fid="${esc(f.id)}">
    <div class="top" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      <span class="badge ${f.type === "bug" ? "bad" : ""}">${esc(KIND[f.type] || f.type)}</span>
      <span class="badge ${f.status === "fixed" ? "hot" : ""}">${esc(STATUS[f.status] || f.status)}</span>
      ${f.rating ? `<span class="muted">rated ${f.rating}/5</span>` : ""}
      ${f.mine ? `<span class="badge">yours</span>` : ""}
      <span class="muted" style="margin-left:auto">${esc(f.date)}${f.page ? " · " + esc(f.page) : ""}</span></div>
    ${f.mode === "guided" ? q("Trying to", f.trying) + q("What happened", f.happened) + q("Expected", f.expected)
                          : `<div style="margin-top:6px">${esc(f.message)}</div>`}
    ${f.reply ? `<div class="reply"><b>Reply</b> <span class="muted">${esc(f.reply_date || "")}</span><div>${esc(f.reply)}</div></div>` : ""}
    ${controls ? `<div class="row">
      ${f.mine || admin ? `<button class="plain small" data-fb="withdraw">${f.mine ? "Withdraw" : "Remove"}</button>` : ""}
      ${admin ? `<select class="small" data-fb="status" aria-label="Status">${Object.keys(STATUS).map((k) => `<option value="${k}" ${k === f.status ? "selected" : ""}>${STATUS[k]}</option>`).join("")}</select>
        <button class="plain small" data-fb="reply">Reply</button>` : ""}
      ${me && me.role === "superadmin" ? `<button class="plain small" data-fb="reveal">Who sent this?</button>` : ""}
    </div><div class="revealed muted"></div>` : ""}
  </div>`;
}

async function loadBoard() {
  const q = new URLSearchParams();
  if ($("#bType").value) q.set("type", $("#bType").value);
  if ($("#bStatus").value) q.set("status", $("#bStatus").value);
  try {
    const items = await api(`/feedback?${q}`);
    $("#boardList").innerHTML = items.map((f) => feedbackCard(f)).join("") || `<p class="muted">No feedback yet. Be the first.</p>`;
  } catch (e) { $("#boardList").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
$("#bType").onchange = loadBoard; $("#bStatus").onchange = loadBoard;

$("#boardList").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-fb]"); if (!b) return;
  const card = b.closest("[data-fid]"), fid = card.dataset.fid;
  try {
    if (b.dataset.fb === "withdraw") {
      if (!confirm("Remove this feedback for everyone?")) return;
      await api(`/feedback/${fid}`, { method: "DELETE" }); toast("Removed"); loadBoard();
    } else if (b.dataset.fb === "reply") {
      const text = prompt("Public reply (names and contact details are removed):");
      if (text === null) return;
      await api(`/admin/feedback/${fid}`, { method: "PATCH", json: { reply: text } }); loadBoard();
    } else if (b.dataset.fb === "reveal") {
      if (!confirm("Look up who sent this? It's recorded in the audit log (by feedback number only).")) return;
      const who = await api(`/super/feedback/${fid}/identity`);
      card.querySelector(".revealed").textContent =
        `Sent by ${who.user || "someone not signed in"} · ${new Date(who.ts * 1000).toLocaleString()} · ${who.ip || "?"} · ${who.agent || "?"}` +
        (who.debug?.recent_errors?.length ? ` · recent errors: ${who.debug.recent_errors.map((x) => x.action + ": " + x.error).join("; ")}` : "");
    }
  } catch (err) { toast(err.message); }
});
$("#boardList").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-fb=status]"); if (!sel) return;
  try { await api(`/admin/feedback/${sel.closest("[data-fid]").dataset.fid}`, { method: "PATCH", json: { status: sel.value } }); toast("Saved"); loadBoard(); }
  catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- admin
function canManage(u) {
  if (u.username === me.username) return false;
  return me.role === "superadmin" || RANK[u.role] < RANK[me.role];
}

async function loadAdmin() {
  try {
    const users = await api("/admin/users");
    $("#users").innerHTML = users.map((u) => {
      const m = canManage(u), sup = me.role === "superadmin" && m;
      const badges = [`<span class="badge ${u.role !== "user" ? "hot" : ""}">${esc(u.role)}</span>`,
        u.disabled && `<span class="badge bad">off</span>`, u.beta && `<span class="badge">beta</span>`,
        u.debug && `<span class="badge bad">debug</span>`, u.must_change && `<span class="badge">temp password</span>`].filter(Boolean).join(" ");
      return `<div class="user"><div class="top"><b>${esc(u.username)}</b>${badges}</div>
        <div class="muted">${u.app ? esc(u.app) : "no to-do app yet"} · last sign-in ${when(u.last_login)} · ${u.sessions} device(s) · ${u.prints} prints · ${u.errors_7d} errors this week</div>
        <div class="row">
          <button class="plain small" data-u="${esc(u.username)}" data-a="activity">Activity</button>
          ${m ? `<button class="plain small" data-u="${esc(u.username)}" data-a="reset">Reset password</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="signout">Sign out</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="beta" data-v="${!u.beta}">${u.beta ? "Remove beta" : "Make beta"}</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="disabled" data-v="${!u.disabled}">${u.disabled ? "Turn on" : "Turn off"}</button>` : ""}
          ${sup ? `<button class="plain small" data-u="${esc(u.username)}" data-a="debug" data-v="${!u.debug}">${u.debug ? "Debug off" : "Debug on"}</button>
          <select class="small" data-u="${esc(u.username)}" data-a="role">${["user", "admin", "superadmin"].map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select>
          <a class="plain small" href="/super/users/${encodeURIComponent(u.username)}/bundle" style="text-decoration:none;color:inherit;border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-size:13px">Debug bundle</a>
          <button class="go warn small" data-u="${esc(u.username)}" data-a="delete">Delete</button>` : ""}
        </div></div>`;
    }).join("");
    $$("#users [data-a]").forEach((el) => (el.tagName === "SELECT" ? el.onchange = () => userAction(el, el.value) : el.onclick = () => userAction(el)));
  } catch (e) { $("#users").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function userAction(el, value) {
  const u = el.dataset.u, a = el.dataset.a, path = `/admin/users/${encodeURIComponent(u)}`;
  try {
    if (a === "activity") return loadActivity(u);
    if (a === "reset") {
      if (!confirm(`Reset ${u}'s password? They'll be signed out everywhere.`)) return;
      const r = await api(`${path}/reset-password`, { method: "POST" });
      showTemp(u, r.temp_password);
    } else if (a === "signout") {
      const r = await api(`${path}/signout`, { method: "POST" });
      toast(`Signed out ${r.signed_out} device(s)`);
    } else if (a === "delete") {
      if (!confirm(`Delete ${u}? This can't be undone.`)) return;
      await api(path, { method: "DELETE" });
      toast(`Deleted ${u}`);
    } else {
      const v = a === "role" ? value : el.dataset.v === "true";
      if (a === "role" && !confirm(`Make ${u} a ${v}?`)) return loadAdmin();
      await api(path, { method: "PATCH", json: { [a]: v } });
      toast("Saved");
    }
  } catch (e) { toast(e.message); }
  loadAdmin();
}

function showTemp(u, pw) {
  $("#nuOut").innerHTML = `<p>Give <b>${esc(u)}</b> this one-time password. It won't be shown again, and they'll pick their own when they sign in.</p>
    <div class="secret">${esc(pw)}</div>
    <div class="row"><button class="plain small" id="copyTemp">Copy</button></div>`;
  $("#copyTemp").onclick = () => navigator.clipboard?.writeText(pw).then(() => toast("Copied"));
  $("#nuOut").scrollIntoView({ behavior: "smooth", block: "center" });
}

$("#nuGo").onclick = async () => {
  try {
    const r = await api("/admin/users", { method: "POST", json: { username: $("#nuName").value, role: $("#nuRole").value, beta: $("#nuBeta").checked } });
    $("#nuName").value = "";
    showTemp(r.username, r.temp_password);
    loadAdmin();
  } catch (e) { toast(e.message); }
};

function eventRow(e) {
  const t = new Date(e.ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const extra = { ...e }; delete extra.ts;
  return `<div class="ev"><span class="${e.ok ? "" : "err"}">${e.ok ? "·" : "✗"}</span> ${esc(t)} <b>${esc(e.user)}</b> ${esc(e.kind)}/${esc(e.action)}
    ${e.error ? `<span class="err">${esc(e.error)}</span>` : ""}<pre hidden>${esc(JSON.stringify(extra, null, 2))}</pre></div>`;
}
function wireEvents(root) { root.querySelectorAll(".ev").forEach((d) => d.onclick = () => { d.querySelector("pre").hidden = !d.querySelector("pre").hidden; }); }

async function loadActivity(u) {
  $("#activityCard").hidden = false;
  $("#actUser").textContent = u;
  try {
    const evs = await api(`/admin/users/${encodeURIComponent(u)}/activity?limit=100`);
    $("#activity").innerHTML = evs.map(eventRow).join("") || `<p class="muted">No activity yet.</p>`;
    wireEvents($("#activity"));
  } catch (e) { $("#activity").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  $("#activityCard").scrollIntoView({ behavior: "smooth" });
}


// ---------------------------------------------------------------- debug (superadmin)
async function loadDebug() {
  loadDiag();
  try {
    const s = await api("/super/settings");
    $$("[data-server]").forEach((el) => el.checked = !!s[el.dataset.server]);
    $("#announceText").value = s.announcement;
  } catch (e) { toast(e.message); }
  loadEvents();
}

async function loadDiag() {
  $("#diag").textContent = "Checking…";
  try {
    const d = await api("/super/diagnostics");
    const ok = (b) => b ? `<span class="ok">ok</span>` : `<span class="err">problem</span>`;
    const rows = [
      ["Printer", `${ok(d.printer.reachable)} ${esc(d.printer.ip || "no IP set")} · ${d.printer.ms} ms · ${esc(d.printer.label)} mm tape`],
      ["Your to-do app", `${ok(d.todoist.ok)} ${d.todoist.ok ? esc(d.todoist.app) + " · " + d.todoist.projects + " lists" : esc(d.todoist.error)} · ${d.todoist.ms} ms`],
      ["Everyone's apps", Object.entries(d.apps || {}).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ")],
      ["Read-back", `${ok(d.vision.key_set)} ${esc(d.vision.model)}${d.vision.key_set ? "" : " · ANTHROPIC_API_KEY missing"}`],
      ["Errors (24h)", d.counts.errors_24h ? `<span class="err">${d.counts.errors_24h}</span>` : "0"],
      ["Totals", `${d.counts.users} people · ${d.counts.printed} tickets · ${d.counts.scans} scans`],
      ["Auto print", esc(JSON.stringify(d.auto_print_last))],
      ["Storage", `${d.data_dir.used_mb} MB used · ${d.data_dir.disk_free_gb} GB free`],
      ["Server", `v${esc(d.version)} · Python ${esc(d.python)} · up ${Math.round(d.uptime_s / 60)} min`],
      ["Config", Object.entries(d.config).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(" · ")],
    ];
    $("#diag").innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  } catch (e) { $("#diag").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
$("#diagGo").onclick = loadDiag;

$$("[data-server]").forEach((el) => el.onchange = async () => {
  if (el.dataset.server === "printing_paused" && el.checked && !confirm("Pause printing for everyone?")) { el.checked = false; return; }
  try { await api("/super/settings", { method: "PATCH", json: { [el.dataset.server]: el.checked } }); toast("Saved"); boot(); }
  catch (e) { toast(e.message); el.checked = !el.checked; }
});
$("#announceGo").onclick = async () => {
  try { await api("/super/settings", { method: "PATCH", json: { announcement: $("#announceText").value.trim() } }); toast("Banner saved"); boot(); }
  catch (e) { toast(e.message); }
};

async function loadEvents() {
  const q = new URLSearchParams({ limit: "200" });
  if ($("#evUser").value.trim()) q.set("user", $("#evUser").value.trim().toLowerCase());
  if ($("#evKind").value) q.set("kind", $("#evKind").value);
  if ($("#evErr").checked) q.set("errors_only", "true");
  try {
    const evs = await api(`/super/events?${q}`);
    $("#events").innerHTML = evs.map(eventRow).join("") || `<p class="muted">Nothing matches.</p>`;
    wireEvents($("#events"));
  } catch (e) { $("#events").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
$("#evGo").onclick = loadEvents;

boot();
