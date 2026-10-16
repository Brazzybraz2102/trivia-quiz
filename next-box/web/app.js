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
  if (!r.ok) throw Object.assign(new Error(body.detail || `${r.status}`), { status: r.status });
  return body;
}
const OFFLINE = "Can't reach Next Box. Check your connection.";
const errText = (e) => (e.status ? e.message : OFFLINE);  // a fetch that never got an answer has no status
const png = (id) => `/printed/${encodeURIComponent(id)}/png`;  // a ticket never changes once made
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "never";

let toastTimer;
function toast(msg, ms = 1800) {
  $("#toast").textContent = msg;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), ms);
}

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("theme") || ""); $("#theme").value = localStorage.getItem("theme") || ""; } catch {}
$("#theme").onchange = () => { applyTheme($("#theme").value); try { localStorage.setItem("theme", $("#theme").value); } catch {} toast("Saved on this device"); };

// ---------------------------------------------------------------- sign in
function showOnly(id) {
  for (const x of ["login", "mustChange", "appShell"]) $("#" + x).hidden = x !== id;
}

// Coming back from "Sign in with ..." (#settings&signin=...) or opening an invite link (#join=CODE).
// Only messages the app itself sends are shown, so a made-up link can't put words in its mouth.
const SIGNIN_MSGS = ["Sign-in was cancelled.", "That sign-in expired. Try again.", "That sign-in didn't work. Try again.",
  "That sign-in was started somewhere else. Start it again from Settings.",
  "Connected Google", "Connected Microsoft", "Connected TickTick"];
let signinMsg = "", pendingJoin = "";
{
  const hash = location.hash;
  if (/(signin|join)=/.test(hash)) history.replaceState(null, "", location.pathname + location.search);
  const s = hash.match(/signin=([^&]*)/);
  if (s) { let msg = ""; try { msg = decodeURIComponent(s[1]); } catch {} if (SIGNIN_MSGS.includes(msg)) signinMsg = msg; }
  // An invite is kept for this tab until it's used, so it survives signing someone else out first.
  const j = hash.match(/join=([A-Za-z0-9-]{4,20})/);
  try {
    if (j) sessionStorage.setItem("nb_join", j[1].toUpperCase());
    pendingJoin = sessionStorage.getItem("nb_join") || "";
  } catch { pendingJoin = j ? j[1].toUpperCase() : ""; }
}
function forgetJoin() { pendingJoin = ""; try { sessionStorage.removeItem("nb_join"); } catch {} }

function showLogin(msg = "") {
  if (me) return startOver(msg);  // someone was signed in: nothing of theirs may stay on the page
  showOnly("login");
  if (pendingJoin) showJoin(pendingJoin);
  $("#loginMsg").textContent = msg;
  $("#loginMsg").className = "muted";
  if (msg && $("#loginForm").hidden) toast(msg, 4000);
  if (!pendingJoin) $("#user").focus();
}

// Signing out (or the session ending) loads the page again from scratch, so the next person on this
// device never sees the last person's screens, notes or chosen photo. The message survives the reload.
function startOver(msg) {
  try { sessionStorage.setItem("nb_msg", msg); } catch {}
  clearPerson();
  location.replace(location.pathname);
}
async function signOut(box) {
  const say = (m) => { if (box) { box.textContent = m; box.className = "err"; } else toast(m, 4000); };
  const failed = "Couldn't sign out. Check your connection and try again.";
  if (!navigator.onLine) return say(failed);
  try { await api("/auth/logout", { method: "POST" }); }
  catch { return say(failed); }  // still signed in: don't pretend otherwise
  startOver("Signed out.");
}
// Belt and braces for the reload above: wipe what's on screen right away.
function clearPerson() {
  me = null; boot.started = false;
  $$("dialog[open]").forEach((d) => d.close());
  showOnly("login");
  $$("#appShell input:not([type=checkbox]):not([type=range]), #appShell textarea, dialog input, dialog textarea").forEach((el) => { el.value = ""; });
  for (const id of ["#scanResult", "#printResult", "#listResult", "#listBody", "#nowText", "#nowStep", "#todayEvents", "#guideSteps",
    "#jDays", "#slugList", "#winsChart", "#winsTotal", "#hist", "#boardList", "#tagList", "#calList", "#appStatus", "#provFields",
    "#calFields", "#sessions", "#pName", "#pFacts", "#pDeletePanel", "#mePanel", "#suTiles", "#suList", "#suNew", "#users",
    "#activity", "#events", "#diag", "#nuOut", "#ivOut", "#ivList", "#printerList", "#uPeople", "#uTickets", "#udInfo", "#udTempOut",
    "#taskid", "#filterTags", "#histMsg", "#scanPending", "#scanRecentList", "#slugResult",
    "#jFoundList", "#jFoundHead", "#jSearchStatus", "#jBackText", "#linesBody", "#linesMsg", "#jxSummary"]) {
    const el = $(id); if (el) el.innerHTML = "";
  }
  for (const id of ["#printResult", "#listResult", "#todayCard", "#guideCard", "#slugCard", "#slugResult", "#jNew", "#activityCard", "#uDetail",
    "#jFound", "#jBack"]) $(id).hidden = true;
  $("#journal").classList.remove("searching"); jData.clear(); lines = { items: [], opener: null };
  listData = { tasks: [], linked: [], tags: [] }; picked = new Set(); skipped = new Set(); guide = null; showingDone = false;
  conn = null; printOpts = null; update = null; editing = null; suRows = []; suPicked = new Set(); suOpen = null;
  markTab("mylist");
  resetSignIn();
}
function resetSignIn() {
  $("#joinForm").reset(); $("#joinForm").hidden = true; $("#loginForm").hidden = false;
  $("#showJoin").textContent = "Have an invite code? Join";
  $("#joinMsg").textContent = "";
  $("#user").value = ""; $("#pass").value = "";
  resetShow();
}

async function boot() {
  let note = "";
  try { note = sessionStorage.getItem("nb_msg") || ""; sessionStorage.removeItem("nb_msg"); } catch {}
  try { me = await api("/auth/me"); }
  catch (e) {
    // Already in, and it's only a hiccup (offline, server busy): stay as you are.
    if (me) return e.status === 401 ? showLogin("Your session ended. Sign in again.") : toast(errText(e), 4000);
    return showLogin(note || (e.message === "sign in required" ? "" : e.message));
  }
  if (me.must_change) { showOnly("mustChange"); $("#mcCur").focus(); return; }
  showOnly("appShell");
  $("#who").textContent = me.username;
  applyLook();
  if (!boot.started) {  // just signed in: open where they asked to
    boot.started = true;
    const start = me.prefs.start_tab || "mylist";
    if (signinMsg) { toast(signinMsg, 4000); signinMsg = ""; goto("settings"); }
    else if (start === "mylist") markTab("mylist");
    else goto(start);
    if (pendingJoin) toast(`You're signed in as ${me.username}. Sign out to use this invite.`, 6000);
  }
  if (me.role === "superadmin") checkUpdate();
  for (const el of [$("#roleBadge"), $("#whoRole")]) {
    el.textContent = me.role;
    el.classList.toggle("hot", me.role !== "user");
  }
  $("#roleBadge").hidden = me.role === "user";
  $("#betaBadge").hidden = !me.beta;
  $("#announce").hidden = !me.announcement;
  $("#announce").textContent = me.announcement || "";
  $("#paused").hidden = !me.printing_paused;
  showAccountNotes();
  $$("[data-min]").forEach((el) => el.hidden = RANK[me.role] < RANK[el.dataset.min]);
  // Superadmins manage people in the Users tab; household admins keep the simple list here.
  for (const id of ["#peopleCard", "#addCard"]) $(id).hidden = me.role === "superadmin";
  $$("#nuRole option[data-min]").forEach((o) => o.disabled = RANK[me.role] < RANK[o.dataset.min]);
  loadPrefs(me.prefs);
  refreshStatus();
  loadConnection();
  loadLabels();
  loadList();
  loadToday();
  loadGuide();
  $("#noticeText").textContent = me.data_notice;
  $("#noticeWhen").textContent = me.consented ? `You accepted this on ${when(me.consented_at)}.` : "";
  if (!me.consented || me.terms_ok === false) {  // new, or the notice or Terms changed since they agreed
    $("#noticeBody").textContent = me.data_notice;
    $("#noticeChanged").hidden = !me.notice_changed;
    $("#noticeMsg").textContent = "";
    if (!$("#noticeDialog").open) $("#noticeDialog").showModal();
  }
}

$("#noticeOk").onclick = async () => {
  try { await api("/auth/consent", { method: "POST" }); $("#noticeDialog").close(); boot(); }
  catch (e) { $("#noticeMsg").textContent = errText(e); }
};
$("#noticeNo").onclick = () => signOut($("#noticeMsg"));
$("#noticeDialog").addEventListener("cancel", (e) => e.preventDefault());  // agree, or sign out

// ---------------------------------------------------------------- revision mark
api("/version").then((v) => {
  $("#revText").textContent = ["Next Box rev " + v.version, v.build, v.credit].filter(Boolean).join(" · ");
}).catch(() => {});

// ---------------------------------------------------------------- printers & label colors
const SWATCH = { white: "#fff", red: "#e53935", orange: "#fb8c00", yellow: "#fdd835", green: "#43a047", blue: "#1e88e5",
                 pink: "#ec407a", purple: "#8e24aa", clear: "transparent", other: "#9e9e9e" };
const sw = (c) => `<span class="swatch" style="background:${SWATCH[c] || "#ccc"}"></span>`;
let printOpts = null;

async function loadLabels() {
  try { printOpts = await api("/printing-options"); } catch (e) { $("#printersLoaded").textContent = e.message; return; }
  const ps = printOpts.printers;
  // No label printers: nothing to pick here, and every ticket still works on paper or as a preview.
  for (const id of ["#defPrinterRow", "#ruleHead", "#colorRules"]) $(id).hidden = !ps.length;
  $("#printersLoaded").innerHTML = ps.length
    ? "Loaded right now: " + ps.map((p) => `${sw(p.stock_color)}${esc(p.name)} (${esc(p.stock_color)} labels${p.ink === "black_red" ? ", red ink" : ""})`).join(", ")
    : "No label printers yet. Every ticket can still be printed on paper or previewed." + (me.role !== "user" ? " Add one under Admin → Printers." : "");
  if (!ps.length) return;
  // Your usual printer, and what happens when it was removed.
  const saved = me.prefs.default_printer || "", missing = saved && !ps.some((p) => p.id === saved);
  $("#defPrinter").innerHTML = `<option value="">First printer (${esc(ps[0].name)})</option>` + ps.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#defPrinter").value = missing ? "" : saved;
  $("#defPrinterNote").textContent = missing ? `Your usual printer was removed; using ${ps[0].name} for now.` : "Used whenever a color below isn't loaded anywhere.";
  $("#defPrinterNote").classList.toggle("err", !!missing);
  if (missing) refreshStatus();  // the header may still name the removed one
  const rules = me.prefs.color_rules || {};
  // Loaded colors first; clear and other only when they're already someone's choice.
  const loaded = new Set(ps.map((p) => p.stock_color));
  const colorOpts = (cur) => printOpts.colors.filter((c) => loaded.has(c) || !["clear", "other"].includes(c) || c === cur)
    .sort((a, b) => loaded.has(b) - loaded.has(a))
    .map((c) => `<option value="${c}" ${cur === c ? "selected" : ""}>${c} labels${loaded.has(c) ? " (loaded)" : ""}</option>`).join("");
  $("#colorRules").innerHTML = Object.entries(printOpts.reasons).map(([k, label]) => `
    <label class="setting"><div class="label"><b>${esc(label)}</b>
      <span>${rules[k] && rules[k] !== "any" ? (ps.some((p) => p.stock_color === rules[k]) ? "" : `No printer has ${esc(rules[k])} labels loaded right now.`) : ""}</span></div>
      <select data-rule="${esc(k)}" aria-label="${esc(label)} label color">
        <option value="any">usual printer</option>${colorOpts(rules[k])}
      </select></label>`).join("");
  $$("#colorRules select").forEach((sel) => sel.onchange = async () => {
    const next = {}; $$("#colorRules select").forEach((x) => next[x.dataset.rule] = x.value);
    try { me.prefs = await api("/settings", { method: "PATCH", json: { color_rules: next } }); toast("Saved"); loadLabels(); }
    catch (e) { toast(e.message); }
  });
}
$("#defPrinter").onchange = async () => {
  try {
    me.prefs = await api("/settings", { method: "PATCH", json: { default_printer: $("#defPrinter").value } });
    toast("Saved"); refreshStatus(); loadLabels();  // the header names the printer your tickets use
  } catch (e) { toast(errText(e)); }
};

let printerStatus = {};  // printer id → answered (true/false), from the last check; null: couldn't check
const labelName = (id) => printOpts.drivers.brother_ql?.labels?.find((l) => l.id === id)?.name || `${id} mm`;

// The list shows straight away; whether each printer answers fills in when the (slower) check is back.
async function loadPrinters(check = true) {
  try { printOpts = await api("/printing-options"); }
  catch (e) { $("#printerList").textContent = errText(e); return; }
  const ps = printOpts.printers;
  $("#printerList").innerHTML = ps.map((p) => `<div class="user" data-pid="${esc(p.id)}"><div class="top">
      <span class="dot" aria-hidden="true"></span><b>${esc(p.name)}</b> <span class="muted pstate"></span>
      <span class="muted">${esc(printOpts.drivers[p.driver]?.name || p.driver)}</span></div>
    <div class="muted">${esc(p.address)}${p.driver === "brother_ql" ? ` · ${esc(p.model)} · ${esc(labelName(p.label))}` : ""} · ${p.width_px} dots${p.ink === "black_red" ? " · black + red roll" : ""}</div>
    <div class="row">${sw(p.stock_color)}<select class="small" data-p="color" aria-label="Label color for ${esc(p.name)}">${printOpts.colors.map((c) => `<option ${c === p.stock_color ? "selected" : ""}>${c}</option>`).join("")}</select>
      <button class="plain small" data-p="edit">Edit</button>
      <button class="plain small" data-p="preview">Test (preview)</button><button class="plain small" data-p="print">Test print</button>
      <button class="plain small" data-p="remove">Remove</button></div></div>`).join("") || "No printers yet. Add one below.";
  showPrinterStatus();
  if (!$("#npDriver").options.length) {
    const bq = printOpts.drivers.brother_ql;
    $("#npDriver").innerHTML = Object.entries(printOpts.drivers).map(([k, d]) => `<option value="${k}">${esc(d.name)}</option>`).join("");
    $("#npColor").innerHTML = printOpts.colors.map((c) => `<option>${c}</option>`).join("");
    $("#npModel").innerHTML = bq.models.map((m) => `<option ${m === "QL-1110NWB" ? "selected" : ""}>${esc(m)}</option>`).join("");
    $("#npLabel").innerHTML = bq.labels.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
    $("#npDriver").onchange = $("#npModel").onchange = driverHints;
    driverHints();
  }
  if (!check || !ps.length) return;
  api("/admin/printers/status").then((s) => { printerStatus = s; })
    .catch(() => { ps.forEach((p) => { if (!(p.id in printerStatus)) printerStatus[p.id] = null; }); })
    .finally(showPrinterStatus);
}
function showPrinterStatus() {
  $$("#printerList [data-pid]").forEach((row) => {
    const s = printerStatus[row.dataset.pid];
    row.querySelector(".dot").className = "dot" + (s === true ? " on" : s === false ? " off" : "");
    row.querySelector(".pstate").textContent = s === true ? "online" : s === false ? "not answering" : s === null ? "couldn't check" : "checking…";
  });
}
const labelWidth = () => printOpts.drivers.brother_ql.labels.find((l) => l.id === $("#npLabel").value)?.width || 696;
function driverHints() {
  const kind = $("#npDriver").value, d = printOpts.drivers[kind], bq = kind === "brother_ql";
  $("#npHint").textContent = "e.g. " + d.address; $("#npWidth").value = bq ? labelWidth() : d.width;
  $("#npBrother").hidden = !bq;
  $("#npInkRow").hidden = !bq || !$("#npModel").value.startsWith("QL-8");  // only the QL-800 series prints red
  if ($("#npInkRow").hidden) $("#npInk").checked = false;
  $("#npHeightRow").hidden = !["zpl", "tspl"].includes(kind);
}
$("#npLabel").onchange = () => { $("#npWidth").value = labelWidth(); if ($("#npLabel").value !== "62") $("#npInk").checked = false; };
$("#npInk").onchange = () => { if ($("#npInk").checked) { $("#npLabel").value = "62"; $("#npWidth").value = labelWidth(); } };  // DK-22251 is 62 mm

// One form adds a printer or, after Edit, saves changes to it (same id, so people's usual printer stays).
function editPrinter(p) {
  $("#npDriver").value = p.driver;
  if (p.driver === "brother_ql") { $("#npModel").value = p.model; $("#npLabel").value = p.label; }
  driverHints();
  $("#npName").value = p.name; $("#npAddress").value = p.address; $("#npColor").value = p.stock_color;
  $("#npInk").checked = p.ink === "black_red"; $("#npWidth").value = p.width_px; $("#npHeight").value = p.label_height_mm || "";
  $("#npGo").dataset.pid = p.id; $("#npGo").textContent = "Save printer";
  $("#npTitle").textContent = `Edit ${p.name}`; $("#npCancel").hidden = false; $("#npMsg").textContent = "";
  $("#npForm").open = true; $("#npForm").scrollIntoView({ behavior: "smooth", block: "nearest" }); $("#npName").focus();
}
function resetPrinterForm() {
  delete $("#npGo").dataset.pid; $("#npGo").textContent = "Add printer";
  $("#npTitle").textContent = "Add a printer"; $("#npCancel").hidden = true; $("#npMsg").textContent = "";
  $("#npName").value = $("#npAddress").value = $("#npHeight").value = "";
  $("#npColor").value = "white"; $("#npInk").checked = false; $("#npModel").value = "QL-1110NWB"; $("#npLabel").value = "62";
  if (printOpts?.drivers && $("#npDriver").options.length) driverHints();
}
$("#npCancel").onclick = () => { resetPrinterForm(); $("#npForm").open = false; };
$("#npGo").onclick = async () => {
  $("#npMsg").textContent = "";
  const pid = $("#npGo").dataset.pid, kind = $("#npDriver").value;
  const body = { driver: kind, name: $("#npName").value, address: $("#npAddress").value, stock_color: $("#npColor").value,
    ink: $("#npInk").checked ? "black_red" : "black", width_px: Number($("#npWidth").value) || null,
    label_height_mm: Number($("#npHeight").value) || 0 };
  if (kind === "brother_ql") Object.assign(body, { model: $("#npModel").value, label: $("#npLabel").value });
  try {
    await api(pid ? `/admin/printers/${encodeURIComponent(pid)}` : "/admin/printers", { method: pid ? "PATCH" : "POST", json: body });
    toast(pid ? "Printer saved" : "Printer added");
    resetPrinterForm(); if (pid) $("#npForm").open = false;
    loadPrinters(); loadLabels();
  } catch (e) { $("#npMsg").textContent = errText(e); }
};
$("#printerList").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-p]"); if (!b) return;
  const pid = b.closest("[data-pid]").dataset.pid, p = printOpts.printers.find((x) => x.id === pid);
  if (!p) return;
  try {
    if (b.dataset.p === "edit") return editPrinter(p);
    if (b.dataset.p === "remove") {
      const n = p.usual_for || 0;
      if (!confirm(`Remove ${p.name}?` + (n ? ` It's the usual printer for ${n} ${n === 1 ? "person" : "people"}; their tickets will go to the first printer in the list instead.` : ""))) return;
      await api(`/admin/printers/${encodeURIComponent(pid)}`, { method: "DELETE" });
      toast(`Removed ${p.name}`);
      if ($("#npGo").dataset.pid === pid) { resetPrinterForm(); $("#npForm").open = false; }
    } else {
      const real = b.dataset.p === "print";
      if (real && !confirm(`Send a real test print to ${p.name}?`)) return;
      const r = await api(`/admin/printers/${encodeURIComponent(pid)}/test?dry_run=${!real}`, { method: "POST" });
      const sent = r.status === "printed";
      const name = esc(r.printer.name);
      $("#prOut").innerHTML = `<p>${sent ? `Printed on ${name}:` : real ? `Printing is paused or set to preview, so nothing went to ${name}. Here's the preview:` : `Preview for ${name}:`}</p>
        <img src="${png(r.id)}" alt="Test label for ${esc(r.printer.name)}" style="max-width:100%;border:1px solid var(--line);border-radius:6px">`;
      $("#prOut").scrollIntoView({ behavior: "smooth", block: "nearest" });
      if (sent) toast(`Sent to ${r.printer.name}`);
    }
    loadPrinters(false); loadLabels();
  } catch (err) { toast(errText(err)); }
});
$("#printerList").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-p=color]"); if (!sel) return;
  try { await api(`/admin/printers/${encodeURIComponent(sel.closest("[data-pid]").dataset.pid)}`, { method: "PATCH", json: { stock_color: sel.value } }); toast("Saved"); loadPrinters(false); loadLabels(); }
  catch (err) { toast(errText(err)); }
});

// ---------------------------------------------------------------- usage (superadmin)
const REASON_NAMES = { overdue: "overdue", urgent: "urgent", today: "daily", list: "lists", task: "single", note: "notes", new_task: "new tasks", text: "notes" };
async function loadUsage() {
  try {
    const u = await api(`/super/usage?days=${$("#uDays").value}`);
    const t = u.totals;
    $("#uTotals").innerHTML = [["People", `${t.people} (${t.active} active)`], ["Tickets", `${t.tickets} (${t.printed} printed, ${t.tickets - t.printed} previews)`], ["Read-backs", t.scans]]
      .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const n = u.people.filter((p) => !p.consented).length;
    $("#uConsent").innerHTML = n ? `${n} ${n === 1 ? "person hasn't" : "people haven't"} accepted the data notice yet (marked below). <button class="linkish" id="uNoConsent">See who in Users</button>.`
                                 : "Everyone has accepted the data notice.";
    $("#uNoConsent")?.addEventListener("click", () => { $("#suStatus").value = "no_consent"; goto("superusers"); });
    // One bar a day (or a week, for longer periods), described in words too: titles don't show on phones.
    const s = u.series, week = u.bucket === "week", total = (d) => d.tickets + d.scans;
    const day = (iso) => new Date(iso + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" });
    const max = Math.max(0, ...s.map(total)), peak = s.find((d) => total(d) === max);
    $("#uChart").innerHTML = s.map((d) => `<div style="height:${Math.round(total(d) / Math.max(1, max) * 100)}%" title="${week ? "Week of " : ""}${day(d.date)}: ${d.tickets} tickets, ${d.scans} read-backs, ${d.people} ${d.people === 1 ? "person" : "people"}"></div>`).join("");
    $("#uChart").setAttribute("aria-label", `${week ? "Weekly" : "Daily"} tickets and read-backs, ${day(s[0].date)} to ${day(s[s.length - 1].date)}: ` +
      (max ? `most was ${max} ${week ? "in the week of" : "on"} ${day(peak.date)}` : "none yet"));
    $("#uChartCap").innerHTML = `<span>${day(s[0].date)}</span><span>${week ? "per week" : "per day"} · most ${max}</span><span>${day(s[s.length - 1].date)}</span>`;
    $("#uPeople").innerHTML = u.people.map((p) => `<div class="card"><div class="top" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <b>${esc(p.username)}</b><span class="badge ${p.role !== "user" ? "hot" : ""}">${esc(p.role)}</span>${p.beta ? `<span class="badge">beta</span>` : ""}
        <span class="muted" style="margin-left:auto">active ${when(p.last_active)}</span></div>
      <div class="muted">${p.app ? esc(p.app) : "no to-do app"} · ${p.consented ? "notice accepted " + when(p.consented_at) : "<span class='err'>notice not accepted yet</span>"}</div>
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
    <h3>Read-backs</h3>${d.scans.map((r) => `<div class="muted">${esc(r.created_at.replace("T", " "))}: ${r.applied.length} applied, ${r.needs_confirmation.length} needed a tap${r.errors.length ? `, ${r.errors.length} problem${r.errors.length === 1 ? "" : "s"}` : ""}</div>`).join("") || "<p class='muted'>None.</p>"}`;
  $("#uDetail").scrollIntoView({ behavior: "smooth" });
});

// ---------------------------------------------------------------- to-do app connection
let providers = [], conn = null;

async function loadConnection(check = false) {  // check: also ask the app whether the saved sign-in still works
  try {
    [providers, conn] = await Promise.all([providers.length ? providers : api("/providers"), api(check ? "/connection?check=1" : "/connection")]);
  } catch (e) { $("#appStatus").textContent = errText(e); return; }
  const ready = providers.filter((p) => p.status === "ready" || p.status === "oauth");
  renderGuide();  // the "Link your to-do app" step names only the apps on offer
  const spec = providers.find((p) => p.key === conn.provider);
  setAppNames();
  $("#filterHelp").textContent = spec?.filter_help || "Try: all, today, tomorrow, overdue, someday, p1–p4, #tag, or any word.";
  // "New tasks go to: My linked app" only means something while one is linked.
  const opt = $("[data-pref=new_tasks_to] option[value=linked]");
  opt.disabled = !conn.connected;
  opt.textContent = conn.connected ? `My linked app (${conn.name})` : "My linked app (none linked)";
  $("#provCancel").hidden = !conn.connected || !!conn.legacy;
  if (conn.connected && conn.ok === false) {  // linked, but the app no longer accepts the saved sign-in
    $("#appStatus").innerHTML = `<p class="err" role="alert" style="margin:0">${esc(conn.error)}</p>
      <div class="muted">Linked: ${esc(conn.name)}${conn.account ? ` as ${esc(conn.account)}` : ""}. Your tasks are safe in ${esc(conn.name)}.</div>
      <div class="row">${conn.legacy ? "" : `<button class="go small" id="appReconnect">Reconnect</button>
        <button class="plain small" id="appDisconnect">Disconnect</button>`}</div>`;
    $("#appConnect").hidden = !conn.legacy;
  } else if (conn.connected) {
    $("#appStatus").innerHTML = `Connected to <b>${esc(conn.name)}</b>${conn.account ? ` as ${esc(conn.account)}` : ""}.
      ${conn.legacy ? `<div class="muted">Using the Todoist token in the server's .env. Connect here to use your own.</div>` : ""}
      ${(conn.lists || []).length ? `<div class="muted">Lists: ${conn.lists.map(esc).join(", ")}</div>` : ""}
      ${conn.legacy ? "" : `<div class="row"><button class="plain small" id="appDisconnect">Disconnect</button>
        <button class="plain small" id="appChange">Switch app</button></div>`}`;
    $("#appConnect").hidden = !conn.legacy;
    $("#appChange")?.addEventListener("click", () => { $("#appConnect").hidden = false; $("#provSel").focus(); });
  } else {
    $("#appStatus").textContent = "Not connected yet. Pick your app below.";
    $("#appConnect").hidden = false;
  }
  $("#appDisconnect")?.addEventListener("click", async () => {
    if (!confirm("Disconnect your to-do app? Your tasks stay in the app; Next Box just stops reading them.")) return;
    try {
      await api("/connection", { method: "DELETE" }); toast("Disconnected");
      me.prefs = await api("/settings"); loadPrefs(me.prefs);  // new tasks go to My list again
    } catch (e) { toast(errText(e), 4000); }
    loadConnection();
  });
  $("#appReconnect")?.addEventListener("click", () => {
    $("#appConnect").hidden = false;
    if ([...$("#provSel").options].some((o) => o.value === conn.provider)) { $("#provSel").value = conn.provider; renderProviderFields(); }
    ($("#provFields input") || $("#provGo")).focus();
  });
  if (!$("#provSel").options.length) {
    $("#provSel").innerHTML = ready.map((p) => `<option value="${esc(p.key)}">${esc(p.name)}</option>`).join("");
    $("#provSel").onchange = renderProviderFields;
    $("#provOther").innerHTML = (ready.filter((p) => p.also).map((p) => `<p><b>${esc(p.name)}</b>: ${esc(p.also)}.</p>`).join("")) +
      providers.filter((p) => p.status !== "ready" && p.status !== "oauth").map((p) => `<p><b>${esc(p.name)}</b>: ${p.status === "needs_setup" ? "not switched on yet." : "not possible."} ${esc(p.reason)}</p>`).join("");
    renderProviderFields();
  }
}

// Where Print tab words point: new tasks go to My list unless Settings says the linked app.
function setAppNames() {
  const linked = conn?.connected ? conn.name : "";
  $$(".newTasksTo").forEach((el) => el.textContent = linked && me?.prefs?.new_tasks_to === "linked" ? linked : "my list");
  $$(".todaySources").forEach((el) => el.textContent = linked ? `my list and ${linked}` : "my list");
}

function renderProviderFields() {
  const p = providers.find((x) => x.key === $("#provSel").value);
  $("#provGo").textContent = p.status === "oauth" ? `Sign in with ${p.app === "google" ? "Google" : p.app === "microsoft" ? "Microsoft" : "TickTick"}` : "Connect";
  if (p.status === "oauth") {
    $("#provFields").innerHTML = `<p class="muted">You'll sign in on ${esc(p.name)}'s own page, then come straight back here. Next Box never sees your password.</p>`;
    $("#provMsg").textContent = "";
    return;
  }
  $("#provFields").innerHTML = (p.fields || []).map((f) => `<div class="field">
      <label for="pf_${esc(f.name)}">${esc(f.label)}${f.optional ? " (optional)" : ""}</label>
      <input id="pf_${esc(f.name)}" data-field="${esc(f.name)}" type="${f.secret ? "password" : "text"}" autocomplete="off" autocapitalize="none">
      ${f.help ? `<div class="muted">${esc(f.help)}</div>` : ""}</div>`).join("");
  $("#provMsg").textContent = "";
}

$("#provGo").onclick = async () => {
  const chosen = providers.find((x) => x.key === $("#provSel").value);
  if (chosen?.status === "oauth") { location.href = `/connect/${encodeURIComponent(chosen.app)}/tasks/start`; return; }
  const fields = {};
  $$("#provFields [data-field]").forEach((el) => fields[el.dataset.field] = el.value);
  $("#provGo").disabled = true;
  $("#provMsg").textContent = `Checking with ${chosen?.name || "your app"}…`; $("#provMsg").className = "muted";  // it can take a few seconds
  try {
    await api("/connection", { method: "PUT", json: { provider: $("#provSel").value, fields } });
    $$("#provFields input").forEach((el) => el.value = "");
    $("#provMsg").textContent = "";
    toast("Connected"); loadConnection(); loadGuide();
  } catch (e) { $("#provMsg").textContent = errText(e); $("#provMsg").className = "err"; }
  finally { $("#provGo").disabled = false; }
};
$("#provCancel").onclick = () => { $("#appConnect").hidden = true; $("#provMsg").textContent = ""; };
// Opening Settings checks that the linked app still accepts the saved sign-in, so "Connected" is true.
$("#topnav button[data-tab=settings]").addEventListener("click", () => { if (me) loadConnection(true); });

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/login", { method: "POST", json: { username: $("#user").value, password: $("#pass").value } });
    $("#pass").value = ""; $("#loginMsg").textContent = "";
    resetShow();
    boot();
  } catch (err) { $("#loginMsg").textContent = errText(err); $("#loginMsg").className = "err"; }
};
$("#mustForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/password", { method: "POST", json: { current: $("#mcCur").value, new: $("#mcNew").value } });
    $("#mcCur").value = $("#mcNew").value = ""; $("#mcMsg").textContent = "";
    resetShow();
    toast("Password saved");
    boot();
  } catch (err) { $("#mcMsg").textContent = errText(err); }
};
$("#mustForm").addEventListener("input", () => { $("#mcMsg").textContent = ""; });
$("#mcSignOut").onclick = () => signOut($("#mcMsg"));
$("#logout").onclick = () => signOut();

// ---------------------------------------------------------------- tabs
const loaders = { mylist: () => { loadList(); loadToday(); loadGuide(); }, printed: () => loadHistory(), profile: () => loadProfile(),
                  print: () => loadPrintTab(),
                  settings: () => { loadLabels(); loadTags(); loadCalendars(); }, admin: () => { if (me.role !== "superadmin") loadAdmin(); loadPrinters(); loadInvites(); },
                  debug: loadDebug, board: () => loadBoard(), usage: () => loadUsage(), journal: () => loadJournal() };
function markTab(tab) {  // which screen shows, without loading anything
  $$("#topnav button").forEach((x) => { x.classList.toggle("on", x.dataset.tab === tab); x.toggleAttribute("aria-current", x.dataset.tab === tab); if (x.dataset.tab === tab) x.setAttribute("aria-current", "page"); });
  $$(".bottombar [data-go]").forEach((x) => x.classList.toggle("on", x.dataset.go === tab));
  $("#moreBtn").classList.toggle("on", !["mylist", "print", "scan", "journal"].includes(tab));
  $$("section").forEach((s) => s.classList.toggle("on", s.id === tab));
  $("#dryRow").hidden = !["mylist", "print", "printed"].includes(tab);  // only where things get printed
}
$$("#topnav button").forEach((b) => b.onclick = () => {
  if (!me) return;  // nothing to open before signing in
  markTab(b.dataset.tab);
  window.scrollTo(0, 0);
  loaders[b.dataset.tab]?.();
});

let statusNow = null;
async function refreshStatus() {
  const n = refreshStatus.n = (refreshStatus.n || 0) + 1;  // a slow, older answer never overwrites a newer one
  try {
    const s = await api("/status");
    if (n !== refreshStatus.n) return;
    statusNow = s;
    $("#status").textContent = !s.printer ? "Previews · print on paper"
      : `${s.printer} ${s.printer_reachable ? "online" : "offline"}${s.dry_run_forced ? " · preview only" : ""}`;
  } catch (e) { $("#status").textContent = `server: ${e.message}`; }
  applyDry();
}
// "Preview only" is ticked and locked, with the reason, whenever a real print can't happen anyway,
// so nobody is asked "Send this to the printer?" for something that will only be a preview.
function applyDry() {
  const why = statusNow?.printing_paused ? "Printing is paused by an admin."
    : me?.prefs?.always_dry_run ? "Always preview only is on in Settings."
    : statusNow?.dry_run_forced ? "This Next Box makes previews only for now."
    : statusNow && !statusNow.printer ? "No label printer yet: print previews on paper."
    : "";
  if (why) $("#dry").checked = true;
  $("#dry").disabled = !!why;
  $("#dryWhy").textContent = why;
}

// ---------------------------------------------------------------- print
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
// A result is shown where it can be seen: scrolled to, and its heading focused (and announced).
function revealResult(box) {
  box.hidden = false;
  box.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  box.querySelector("h2")?.focus({ preventScroll: true });
}
function resultHeading(r) {
  const all = [r, ...(r.also || [])];
  return all.some((t) => t.status === "failed") ? "Didn't print" : all.some((t) => t.status === "printed") ? "Printed" : "Preview ready";
}
function ticketResult(t) {
  const n = t.manifest.length;
  const what = t.status === "printed" ? "Printed" : t.status === "failed" ? "Didn't print" : "Preview";
  const size = n ? `${n} task${n === 1 ? "" : "s"}` : t.kind === "text" ? "note" : "no tasks";
  return `<p><b>${what}</b> #${esc(t.id)} · ${size}${t.printer ? ` · ${sw(t.printer.color)}${esc(t.printer.name)}` : ""}</p>
    ${t.note ? `<p class="${t.status === "failed" ? "err" : "muted"}">${esc(t.note)}</p>` : ""}
    ${paperButton(t.id)}<img src="${png(t.id)}" alt="Preview of ticket #${esc(t.id)}">`;
}
function showResultError(box, msg) {
  box.innerHTML = `<h2 tabindex="-1">Couldn't print</h2><p class="err">${esc(msg)}</p>`;
  revealResult(box);
}
// While a print is on its way the button says so (and can't be pressed twice); returns "put it back".
function busy(b, dry) {
  const html = b.innerHTML;
  b.disabled = true; b.setAttribute("aria-busy", "true");
  b.textContent = dry ? "Making preview…" : "Printing…";
  return () => { b.disabled = false; b.removeAttribute("aria-busy"); b.innerHTML = html; };
}

function showPrint(r) {
  const box = $("#printResult");
  if (r.status === "skipped") { box.innerHTML = `<h2 tabindex="-1">Skipped</h2><p>${esc(r.reason)}</p>`; return revealResult(box); }
  box.innerHTML = `<h2 tabindex="-1">${resultHeading(r)}</h2>` + [...(r.also || []), r].map(ticketResult).join("<hr>");
  revealResult(box);
}

async function printAct(b) {
  const act = b.dataset.act;
  const text = $("#text").value, task_id = $("#taskid").value;
  if (act === "task" && !task_id) return showResultError($("#printResult"), "Pick a task from your list first.");
  if ((act === "text" || act === "todo") && !text.trim()) return showResultError($("#printResult"), "Type something to print first.");
  const dry_run = $("#dry").checked;
  const routes = {
    today: ["/print/today", { source: "manual", dry_run }],
    list: ["/print/list", { query: $("#query").value, dry_run }],
    text: ["/print/text", { text, dry_run }],
    todo: ["/print/text", { text, todo: true, dry_run }],
    task: ["/print/task", { task_id, dry_run }],
  };
  if (!dry_run && !confirm("Send this to the printer?")) return;
  const done = busy(b, dry_run);
  try {
    showPrint(await api(routes[act][0], { method: "POST", json: routes[act][1] }));
    if (act === "todo") loadPrintTab();  // the new task can be picked under One task now
  }
  catch (e) { showResultError($("#printResult"), errText(e)); }
  finally { done(); }
}
$$("[data-act]").forEach((b) => b.onclick = (e) => { e.preventDefault(); printAct(b); });
$("#listForm").onsubmit = (e) => e.preventDefault();  // Enter in the filter box presses Print list

// The One task picker and the tag shortcuts come from your list, so nobody needs a task ID.
async function loadPrintTab() {
  let r;
  try { r = await api("/list"); } catch { return; }
  const all = [...(r.tasks || []), ...(r.linked || [])];
  const keep = $("#taskid").value;
  $("#taskid").innerHTML = `<option value="">${all.length ? "Pick a task…" : "Your list is empty"}</option>` +
    all.map((t) => `<option value="${esc(t.id)}">${esc(t.content.length > 70 ? t.content.slice(0, 69) + "…" : t.content)}</option>`).join("");
  if (all.some((t) => t.id === keep)) $("#taskid").value = keep;
  $("#filterTags").innerHTML = (r.tags || []).map((g) => `<button type="button" data-filter-tag="#${esc(g.name)}">#${esc(g.name)}</button>`).join("");
}
$("#filterTags").onclick = (e) => {
  const b = e.target.closest("[data-filter-tag]");
  if (b) { $("#query").value = b.dataset.filterTag; $("#query").focus(); }
};

// ---------------------------------------------------------------- read back
const VERB = { drop: "Delete", done: "Complete", tomorrow: "Move to tomorrow" };
const MARK_STATUS = { applied: "Done ✓", skipped: "Skipped", error: "Couldn't apply", undone: "Undone", already: "Already done",
                      applying: "Working on it…", undoing: "Undoing…" };
const scanNotes = {};  // notes read from a photo, shown with it here (they're kept only in your journal)
let scanFile = null;
const shortTime = (iso) => new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
const waitingIn = (r) => r.needs_confirmation.filter((p) => p.status === "pending");

function ignoredLine(i) {
  const what = `${VERB[i.mark] || ""} ${i.content || ""}`.trim();
  return { "not on this label": `Row ${i.row} isn't on this ticket`, "duplicate row": `Row ${i.row} was read twice`,
           "already done": `Already done: ${i.content}`, "already moved": `Already moved: ${i.content}`,
           "read before": `Already read from this ticket: ${what}` }[i.reason] || `Row ${i.row}: ${i.reason}`;
}

function scanCard(rec) {
  if (rec.notes !== undefined) scanNotes[rec.id] = { notes: rec.notes, win: rec.win || "" };
  const kept = scanNotes[rec.id] || {};
  const conflict = rec.label_conflict;
  const pend = waitingIn(rec);
  let h = `<div class="card scanrec" data-scan="${esc(rec.id)}">
    <h2 tabindex="-1">${rec.label_id ? `Read back: ticket #${esc(rec.label_id)}` : "Read back"}</h2>
    <p class="muted">${esc(shortTime(rec.created_at))}</p>`;
  if (rec.previous_scan) h += `<p class="muted">You read this ticket back earlier (${esc(shortTime(rec.previous_scan.created_at))}). Marks from then were left alone.</p>`;
  if (conflict) h += `<div class="pending"><b>The photo shows #${esc(conflict.read)} but you typed #${esc(conflict.typed)}. Which one?</b>
      <p class="muted">Nothing changes until you pick.</p>
      <div class="row"><button class="go" data-label="${esc(conflict.read)}">#${esc(conflict.read)} (in the photo)</button>
      <button class="plain" data-label="${esc(conflict.typed)}">#${esc(conflict.typed)} (typed)</button></div></div>`;
  rec.errors.forEach((e) => h += `<p class="err">${esc(e)}</p>`);
  if (rec.status === "failed" && rec.journal_id) h += `<div class="row"><button class="go" data-retry>Try again</button></div>`;
  if (rec.candidates) {
    h += rec.candidates.length ? `<div class="tickets">${rec.candidates.map((c) => `<button data-label="${esc(c.id)}">
        <img src="/printed/${encodeURIComponent(c.id)}/png" alt="" loading="lazy">
        <span><b>#${esc(c.id)}</b><br>${esc(shortTime(c.created_at))} · ${c.rows} task${c.rows === 1 ? "" : "s"}</span></button>`).join("")}</div>`
      : `<p class="muted">You haven't printed a ticket with tasks yet.</p>`;
    h += `<details style="margin-top:8px"><summary class="muted">Type the code instead</summary>
        <div class="row"><input type="text" data-code-input aria-label="Ticket code" autocapitalize="characters" spellcheck="false" placeholder="e.g. 260930-P9AM or P9AM" style="max-width:240px">
        <button class="plain" data-code-go>Use this code</button></div></details>`;
  }
  rec.applied.forEach((a) => h += a.status === "undone"
    ? `<p class="muted">Undone: ${esc(VERB[a.mark])} ${esc(a.content)}</p>`
    : `<div class="done-row"><span class="applied">✓ ${esc(VERB[a.mark])}: ${esc(a.content)}</span>${a.status === "undoing" ? `<span class="muted">Undoing…</span>`
        : `<button class="linkish" data-undo="${a.row}" aria-label="Undo: ${esc(VERB[a.mark])} ${esc(a.content)}">Undo</button>`}</div>`);
  rec.needs_confirmation.filter((p) => p.status !== "pending").forEach((p) => h += p.status === "applied" && p.mark !== "drop"
    ? `<div class="done-row"><span class="applied">✓ ${esc(VERB[p.mark])}: ${esc(p.content)}</span><button class="linkish" data-undo="${p.row}" aria-label="Undo: ${esc(VERB[p.mark])} ${esc(p.content)}">Undo</button></div>`
    : `<p class="${p.status === "error" ? "err" : "muted"}">${esc(p.reason === "already moved" ? "Already moved" : MARK_STATUS[p.status] || p.status)}: ${esc(VERB[p.mark])} ${esc(p.content)}${p.status === "error" && p.error ? ` (${esc(p.error)})` : ""}</p>`);
  pend.forEach((p) => h += `<div class="pending"><b>${esc(VERB[p.mark])}?</b> ${esc(p.content)}
      <div class="muted">row ${p.row} · ${Math.round(p.confidence * 100)}% sure${p.note ? " · " + esc(p.note) : ""}</div>
      ${conflict ? "" : `<div class="row"><button class="go ${p.mark === "drop" ? "warn" : ""}" data-row="${p.row}" data-d="confirm" aria-label="Confirm: ${esc(VERB[p.mark])} ${esc(p.content)}">Confirm</button>
      <button class="plain" data-row="${p.row}" data-d="skip" aria-label="Skip ${esc(p.content)}">Skip</button></div>`}</div>`);
  if (pend.length > 1 && !conflict) h += `<div class="row"><button class="linkish" data-skip-all>Skip all ${pend.length}</button></div>`;
  (rec.ignored || []).forEach((i) => h += `<p class="ignored">${esc(ignoredLine(i))}</p>`);
  if (rec.message) h += `<p class="applied">${esc(rec.message)}</p>`;
  if (kept.win) h += `<p class="jwin"><b>Your win:</b> ${esc(kept.win)}</p>`;
  if (kept.notes) h += `<h3>Your notes</h3><p style="white-space:pre-wrap">${esc(kept.notes)}</p>
      <div class="row"><button class="plain small" data-lines>Add lines to my list</button></div>`;
  if (rec.journal_id) h += `<p class="muted">Saved to your journal. <button class="linkish" data-go-tab="journal">Open Journal</button></p>`;
  if (!rec.applied.length && !rec.needs_confirmation.length && !rec.errors.length && !rec.journal_id && !(rec.ignored || []).length) h += `<p class="muted">No marks found.</p>`;
  return h + `<p class="err" data-msg role="alert"></p></div>`;
}

function makeScanCard(rec) {
  const box = document.createElement("div");
  box.innerHTML = scanCard(rec);
  const card = box.firstElementChild;
  wireScanCard(card, rec);
  return card;
}

function wireScanCard(card, rec) {
  const act = async (btn, path, json) => {
    card.querySelectorAll("button").forEach((b) => b.disabled = true);
    try {
      const fresh = makeScanCard(await api(`/scan/${encodeURIComponent(rec.id)}/${path}`, { method: "POST", json }));
      card.replaceWith(fresh);
      fresh.querySelector("h2").focus({ preventScroll: true });
      loadWaiting(); loadRecent();
    } catch (e) {
      card.querySelectorAll("button").forEach((b) => b.disabled = false);
      card.querySelector("[data-msg]").textContent = errText(e);
    }
  };
  card.querySelectorAll("[data-row]").forEach((b) => b.onclick = () => act(b, "confirm", { decisions: { [b.dataset.row]: b.dataset.d } }));
  card.querySelector("[data-skip-all]")?.addEventListener("click", (e) =>
    act(e.target, "confirm", { decisions: Object.fromEntries(waitingIn(rec).map((p) => [p.row, "skip"])) }));
  card.querySelectorAll("[data-undo]").forEach((b) => b.onclick = () => act(b, "undo", { row: Number(b.dataset.undo) }));
  card.querySelectorAll("[data-label]").forEach((b) => b.onclick = () => act(b, "label", { label_id: b.dataset.label }));
  card.querySelector("[data-retry]")?.addEventListener("click", (e) => {
    card.querySelector("[data-msg]").innerHTML = `<span class="muted"><span class="spin" aria-hidden="true"></span>Reading your page again…</span>`;
    act(e.target, "retry");
  });
  card.querySelector("[data-lines]")?.addEventListener("click", (e) => openLines(scanNotes[rec.id]?.notes || "", e.currentTarget));
  const code = card.querySelector("[data-code-input]");
  card.querySelector("[data-code-go]")?.addEventListener("click", (e) => code.value.trim() && act(e.target, "label", { label_id: code.value.trim() }));
  code?.addEventListener("keydown", (e) => { if (e.key === "Enter") card.querySelector("[data-code-go]").click(); });
}

function renderScan(rec) {
  const card = makeScanCard(rec);
  $("#scanResult").replaceChildren(card);
  $(`#scanPending [data-scan="${CSS.escape(rec.id)}"]`)?.remove();  // shown above now
  card.querySelector("h2").focus({ preventScroll: true });
  card.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

// How many marks from photos are waiting for a tap: on the Snap button and at the top of My list.
function setWaiting(n) {
  $$("[data-waiting]").forEach((b) => {
    b.hidden = !n;
    b.innerHTML = n ? `<span class="sr-only">, </span>${n}<span class="sr-only"> waiting</span>` : "";
  });
  $("#waitingNote").hidden = !n;
  $("#waitingText").textContent = n === 1 ? "1 mark from a photo is waiting for you." : `${n} marks from photos are waiting for you.`;
}

async function loadWaiting(render = false) {
  let recs;
  try { recs = await api("/scans?pending=true&limit=20"); } catch { return; }
  setWaiting(recs.reduce((n, r) => n + waitingIn(r).length, 0));
  if (!render) return;
  const shown = $("#scanResult [data-scan]")?.dataset.scan;
  const others = recs.filter((r) => r.id !== shown);
  $("#scanPending").innerHTML = others.length ? `<h2 class="sectionhead">Still waiting for you</h2>` : "";
  others.forEach((r) => $("#scanPending").append(makeScanCard(r)));
}

async function loadRecent() {
  let recs;
  try { recs = await api("/scans?limit=8"); } catch { return; }
  $("#scanRecent").hidden = !recs.length;
  $("#scanRecentList").innerHTML = recs.map((r) => {
    const applied = r.applied.filter((a) => a.status !== "undone").length + r.needs_confirmation.filter((p) => p.status === "applied").length;
    const bits = [r.label_id ? `ticket #${r.label_id}` : "no ticket", `${applied} applied`,
                  waitingIn(r).length ? `${waitingIn(r).length} waiting` : "", r.status === "failed" ? "couldn't read it" : ""].filter(Boolean);
    return `<button class="recent" data-open-scan="${esc(r.id)}"><b>${esc(shortTime(r.created_at))}</b><span class="muted">${esc(bits.join(" · "))}</span></button>`;
  }).join("");
}
$("#scanRecentList").onclick = async (e) => {
  const b = e.target.closest("[data-open-scan]");
  if (!b) return;
  try { renderScan(await api(`/scan/${encodeURIComponent(b.dataset.openScan)}`)); } catch (err) { toast(errText(err)); }
};
loaders.scan = () => { loadWaiting(true); loadRecent(); };

// Picking the photo: the camera, or one already taken (the night-time journal photo, a screenshot).
function choosePhoto(file) {
  scanFile = file || null;
  const img = $("#scanPreview");
  if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
  delete img.dataset.url;
  img.removeAttribute("src");
  $("#scanChosen").hidden = !scanFile;
  $("#scanNoPreview").hidden = true;
  $("#scanGo").disabled = !scanFile;
  if (!scanFile) { $("#photo").value = ""; $("#photoCam").value = ""; return; }
  img.hidden = false;
  img.dataset.url = URL.createObjectURL(scanFile);
  img.src = img.dataset.url;
  $("#scanName").textContent = scanFile.name || "Your photo";
}
$("#scanPreview").onerror = () => { if (!scanFile) return; $("#scanPreview").hidden = true; $("#scanNoPreview").hidden = false; };
$("#snapCam").onclick = () => $("#photoCam").click();
$("#snapLib").onclick = () => $("#photo").click();
$("#photoCam").onchange = (e) => choosePhoto(e.target.files[0]);
$("#photo").onchange = (e) => choosePhoto(e.target.files[0]);
$("#scanRetake").onclick = () => { choosePhoto(null); $("#snapCam").focus(); };

$("#scanGo").onclick = async () => {
  if (!scanFile) return;
  const fd = new FormData();
  fd.append("photo", scanFile);
  if ($("#scanLabel").value.trim()) fd.append("label_id", $("#scanLabel").value.trim().replace(/^#/, ""));
  $("#scanGo").disabled = true;
  $("#scanResult").innerHTML = `<p class="muted"><span class="spin" aria-hidden="true"></span>Reading your page… this can take up to a minute.</p>`;
  try {
    const rec = await api("/scan", { method: "POST", body: fd });
    renderScan(rec);
    if (!(rec.status === "failed" && !rec.journal_id)) {  // read (or kept to try again): one more tap mustn't read it twice
      choosePhoto(null);
      $("#scanLabel").value = ""; $("#scanCode").open = false;
    }
    loadWaiting(true); loadRecent();
  } catch (e) { $("#scanResult").innerHTML = `<p class="err">${esc(errText(e))}</p>`; }
  finally { $("#scanGo").disabled = !scanFile; }
};

// ---------------------------------------------------------------- history
const KIND_NAMES = { today: "Daily label", focus: "Just one thing", stickers: "Stickers", strips: "Tear-off strips",
                     list: "List", task: "One task", text: "Note" };
const HIST_PAGE = 30;
let histItems = [];
function ticketWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const old = Date.now() - d.getTime() > 6 * 864e5;  // this week: "Thu 3:05 PM"; older: "Oct 2, 3:05 PM"
  return d.toLocaleString([], old ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
                                  : { weekday: "short", hour: "numeric", minute: "2-digit" });
}
// What "Print again" sends: the same task(s) or note again, as a fresh ticket.
function againRoute(r) {
  const ids = (r.manifest || []).map((m) => m.task_id);
  if ((r.kind === "task" || r.kind === "focus") && ids.length) return [`/print/${r.kind}`, { task_id: ids[0] }];
  if ((r.kind === "stickers" || r.kind === "strips") && ids.length) return [`/print/${r.kind}`, { task_ids: ids }];
  if (r.kind === "text" && r.text) return ["/print/text", { text: r.text, title: r.title }];
  return null;
}
function histCard(r) {
  const title = r.title.length > 50 ? r.title.slice(0, 49) + "…" : r.title;
  const state = r.failed ? " · didn't print" : r.dry_run ? " · preview" : "";
  return `<div class="card" data-id="${esc(r.id)}">
    <div class="item"><b>${esc(title)}</b><span class="muted">#${esc(r.id)}</span></div>
    <div class="muted">${esc(ticketWhen(r.created_at))} · ${esc(KIND_NAMES[r.kind] || r.kind)}${r.by && r.by !== me?.username ? ` · ${esc(r.by)}` : ""}${state}</div>
    ${paperButton(r.id)}
    ${againRoute(r) ? `<div class="row"><button class="plain small" data-again>Print again</button></div>` : ""}
    <img loading="lazy" src="${png(r.id)}" alt="Ticket #${esc(r.id)}: ${esc(r.title)}"></div>`;
}
async function loadHistory(more = false) {
  if (!more) { histItems = []; $("#histMsg").textContent = ""; }
  try {
    const items = await api(`/printed?limit=${HIST_PAGE}&offset=${histItems.length}`);
    histItems.push(...items);
    if (more) $("#hist").insertAdjacentHTML("beforeend", items.map(histCard).join(""));
    else $("#hist").innerHTML = items.length ? items.map(histCard).join("")
      : `<p class="muted">Nothing printed yet. Try Print → Print today, or My list → Stickers.</p>`;
    $("#histOlder").hidden = items.length < HIST_PAGE;
  } catch (e) { $("#histMsg").className = "err"; $("#histMsg").textContent = errText(e); }
}
$("#histOlder").onclick = () => loadHistory(true);
$("#hist").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-again]");
  if (!b) return;
  const route = againRoute(histItems.find((x) => x.id === b.closest("[data-id]").dataset.id) || {});
  if (!route) return;
  const dry_run = $("#dry").checked;
  if (!dry_run && !confirm("Send this to the printer?")) return;
  const done = busy(b, dry_run);
  try {
    const t = await api(route[0], { method: "POST", json: { ...route[1], dry_run } });
    await loadHistory();
    window.scrollTo({ top: 0, behavior: reducedMotion() ? "auto" : "smooth" });
    $("#histMsg").className = t.status === "failed" ? "err" : "muted";
    $("#histMsg").textContent = t.status === "failed" ? t.note
      : `${t.status === "printed" ? "Printed again" : "New preview made"}: #${t.id}, at the top.`;
  } catch (err) { $("#histMsg").className = "err"; $("#histMsg").textContent = errText(err); done(); }
});

// ---------------------------------------------------------------- settings
function confText(v) { $("#confLabel").textContent = `Marks below ${Math.round(v * 100)}% wait for you.`; }

function loadPrefs(prefs) {
  $$("[data-pref]").forEach((el) => {
    if (el.dataset.bad) return;  // keeps what they typed until it's fixed (see prefErr)
    const v = prefs[el.dataset.pref];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  confText(prefs.confidence);
  $("#dry").checked = true;
  applyDry();
  setAppNames();
  // The Print tab's filter follows the default, unless they typed their own there.
  const q = $("#query");
  if (!q.value || q.value === loadPrefs.lastFilter) q.value = prefs.default_filter || "";
  loadPrefs.lastFilter = prefs.default_filter || "";
}

// A typed setting that wasn't saved: say why next to it, in red, and keep what they typed.
function prefErr(el, msg) {
  const label = el.closest(".setting")?.querySelector(".label");
  let box = label?.querySelector(".err");
  if (!msg) { box?.remove(); delete el.dataset.bad; el.removeAttribute("aria-invalid"); return; }
  if (!label) return toast(msg, 4000);
  if (!box) { box = document.createElement("span"); box.className = "err"; box.setAttribute("role", "alert"); label.append(box); }
  box.textContent = msg; el.dataset.bad = "1"; el.setAttribute("aria-invalid", "true");
}

$$("[data-pref]").forEach((el) => el.onchange = async () => {
  const k = el.dataset.pref, typed = el.type === "text" || el.type === "number";
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number") v = parseInt(v, 10);
  if (el.type === "range") v = parseFloat(v);
  try {
    const prefs = await api("/settings", { method: "PATCH", json: { [k]: v } });
    if (me) { me.prefs = prefs; applyLook(); }
    if (typed) prefErr(el, "");
    loadPrefs(prefs);
    if (k === "always_dry_run") refreshStatus();  // the header and the Preview box follow it
    toast("Saved");
  } catch (e) {
    if (typed) prefErr(el, errText(e));
    else { toast(errText(e), 4000); loadPrefs(me.prefs); }
  }
});
$("[data-pref=confidence]").oninput = (e) => confText(e.target.value);

$("#pwForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/password", { method: "POST", json: { current: $("#pwCur").value, new: $("#pwNew").value } });
    $("#pwCur").value = $("#pwNew").value = "";
    resetShow();
    $("#pwMsg").textContent = "Changed. Your other devices were signed out."; $("#pwMsg").className = "muted";
    loadSessions();
  } catch (err) { $("#pwMsg").textContent = errText(err); $("#pwMsg").className = "err"; }
};
$("#pwForm").addEventListener("input", () => { $("#pwMsg").textContent = ""; });

// "Safari on iPhone", "Chrome on Windows", "Next Box app": enough to recognise a lost phone.
function deviceName(agent) {
  const a = agent || "";
  if (/^NextBox\//i.test(a)) return "Next Box app";
  const browser = /Edg(e|A|iOS)?\//.test(a) ? "Edge" : /OPR\/|Opera/.test(a) ? "Opera" : /Firefox\/|FxiOS/.test(a) ? "Firefox"
    : /Chrome\/|CriOS/.test(a) ? "Chrome" : /Safari\//.test(a) ? "Safari" : "";
  const os = /iPhone/.test(a) ? "iPhone" : /iPad/.test(a) ? "iPad" : /Android/.test(a) ? "Android" : /CrOS/.test(a) ? "Chromebook"
    : /Mac OS X|Macintosh/.test(a) ? "Mac" : /Windows/.test(a) ? "Windows" : /Linux/.test(a) ? "Linux" : "";
  if (browser && os) return `${browser} on ${os}`;
  return browser || os || "Unknown device";
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 900) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} minutes ago`;
  if (s < 86400) { const h = Math.round(s / 3600); return `${h} hour${h === 1 ? "" : "s"} ago`; }
  return when(ts);
}
async function loadSessions() {
  try {
    const list = await api("/auth/sessions");
    $("#sessions").innerHTML = list.map((s) => `<div class="device" data-sid="${esc(s.id)}"><div style="flex:1;min-width:0">
        <b>${esc(deviceName(s.agent))}</b>${s.current ? ` <span class="badge hot">this device</span>` : ""}
        <div class="muted">${s.current ? "In use now" : "Last used " + esc(ago(s.last_used))} · ${esc(s.ip || "?")} · signed in ${esc(when(s.created))}</div></div>
        ${s.current ? "" : `<button class="plain small" data-endsession aria-label="Sign out ${esc(deviceName(s.agent))}">Sign out</button>`}</div>`).join("")
      || `<p class="muted">None.</p>`;
  } catch (e) { $("#sessions").textContent = errText(e); }
}
$("#sessions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-endsession]"); if (!b) return;
  b.disabled = true;
  try { await api(`/auth/sessions/${encodeURIComponent(b.closest("[data-sid]").dataset.sid)}`, { method: "DELETE" }); toast("Signed out that device"); }
  catch (err) { toast(errText(err), 4000); }
  loadSessions();
});
$("#signoutOthers").onclick = async () => {
  const r = await api("/auth/signout-others", { method: "POST" });
  toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? "" : "s"}`);
  loadSessions();
};

// ---------------------------------------------------------------- feedback (anonymous)
let fbMode = "guided";
const currentPage = () => (me ? document.querySelector("#topnav button.on")?.dataset.tab : "login") || "";

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
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) $(`#topnav button[data-tab=${g.dataset.goto}]`).click(); });
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
    if (document.querySelector("#topnav button.on")?.dataset.tab === "board") loadBoard();
  } catch (e) { $("#fbErr").textContent = e.message; }
  finally { $("#fbSend").disabled = false; }
};

const KIND = { bug: "Something broke", confusing: "Confusing", idea: "Idea", praise: "Love it", other: "Other" };
const STATUS = { new: "new", seen: "seen", planned: "planned", fixed: "fixed", wontfix: "won't fix", hidden: "hidden" };

function feedbackCard(f, controls = true) {
  const admin = controls && me && me.role === "superadmin";  // moderation is staff-only: the board spans households
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

let boardItems = new Map();  // feedback id → item, so Reply starts from the current reply
async function loadBoard() {
  // Staff can look at just the posts they've hidden (they see them under "any status" too).
  if (me?.role === "superadmin" && !$("#bStatus option[value=hidden]")) $("#bStatus").insertAdjacentHTML("beforeend", `<option value="hidden">hidden</option>`);
  const q = new URLSearchParams();
  if ($("#bType").value) q.set("type", $("#bType").value);
  if ($("#bStatus").value) q.set("status", $("#bStatus").value);
  try {
    const items = await api(`/feedback?${q}`);
    boardItems = new Map(items.map((f) => [f.id, f]));
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
    } else if (b.dataset.fb === "reply") {  // edit in place, starting from the reply that's there
      if (!card.querySelector(".replyEdit")) {
        card.querySelector(".revealed").insertAdjacentHTML("beforebegin", `<div class="replyEdit field">
          <label for="reply-${esc(fid)}">Public reply</label><span class="muted">Names and contact details are removed.</span>
          <textarea id="reply-${esc(fid)}">${esc(boardItems.get(fid)?.reply || "")}</textarea>
          <div class="row"><button class="go small" data-fb="replySave">Save reply</button><button class="plain small" data-fb="replyCancel">Cancel</button></div></div>`);
      }
      card.querySelector(".replyEdit textarea").focus();
    } else if (b.dataset.fb === "replyCancel") {
      card.querySelector(".replyEdit").remove();
    } else if (b.dataset.fb === "replySave") {
      const text = card.querySelector(".replyEdit textarea").value.trim(), had = !!boardItems.get(fid)?.reply;
      if (!text && !had) return card.querySelector(".replyEdit").remove();
      if (!text && !confirm("Remove the reply?")) return;
      await api(`/admin/feedback/${fid}`, { method: "PATCH", json: { reply: text } });
      toast(text ? "Reply saved" : "Reply removed"); loadBoard();
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
// Phones stay signed in for weeks, so "last active" (using the app) says more than the last sign-in.
const lastActive = (u) => Math.max(u.last_seen || 0, u.last_login || 0) || null;

function canManage(u) {
  if (u.username === me.username) return false;
  return me.role === "superadmin" || RANK[u.role] < RANK[me.role];
}

// Household admins only: superadmins manage people in the Users tab (openUser).
async function loadAdmin() {
  try {
    const users = await api("/admin/users");
    $("#users").innerHTML = users.map((u) => {
      const m = canManage(u);
      const badges = [`<span class="badge ${u.role !== "user" ? "hot" : ""}">${esc(u.role)}</span>`,
        u.disabled && `<span class="badge bad">off</span>`, u.beta && `<span class="badge">beta</span>`,
        u.debug && `<span class="badge bad">debug</span>`, u.must_change && `<span class="badge">temp password</span>`].filter(Boolean).join(" ");
      return `<div class="user"><div class="top"><b>${esc(u.username)}</b>${badges}</div>
        <div class="muted">${u.app ? esc(u.app) : "no to-do app yet"} · last active ${when(lastActive(u))} · ${u.sessions} device(s) · ${u.prints} prints · ${u.errors_7d} errors this week</div>
        <div class="row">
          <button class="plain small" data-u="${esc(u.username)}" data-a="activity">Activity</button>
          ${m ? `<button class="plain small" data-u="${esc(u.username)}" data-a="reset">Reset password</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="signout">Sign out</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="beta" data-v="${!u.beta}">${u.beta ? "Remove beta" : "Make beta"}</button>
          <button class="plain small" data-u="${esc(u.username)}" data-a="disabled" data-v="${!u.disabled}">${u.disabled ? "Turn on" : "Turn off"}</button>` : ""}
        </div></div>`;
    }).join("");
    $$("#users [data-a]").forEach((el) => el.onclick = () => userAction(el));
  } catch (e) { $("#users").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function userAction(el) {
  const u = el.dataset.u, a = el.dataset.a, path = `/admin/users/${encodeURIComponent(u)}`;
  try {
    if (a === "activity") return loadActivity(u);
    if (a === "reset") {
      if (!confirm(`Reset ${u}'s password? They'll be signed out everywhere.`)) return;
      const r = await api(`${path}/reset-password`, { method: "POST" });
      showTemp(u, r.temp_password);
    } else if (a === "signout") {
      if (!confirm(`Sign ${u} out on every device? They can sign straight back in.`)) return;
      const r = await api(`${path}/signout`, { method: "POST" });
      toast(`Signed out ${r.signed_out} device(s)`);
    } else {
      const v = el.dataset.v === "true";
      if (a === "disabled" && v && !confirm(`Turn off ${u}? They'll be signed out everywhere and can't sign in until you turn them back on.`)) return;
      await api(path, { method: "PATCH", json: { [a]: v } });
      toast("Saved");
    }
  } catch (e) { toast(errText(e)); }
  loadAdmin();
}

// Copy, also where the clipboard API isn't offered (Next Box opened at a plain http:// LAN address).
function copyText(text) {
  const done = () => toast("Copied"), manual = () => toast("Couldn't copy. Press and hold it to copy instead.", 4000);
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text).then(done, manual);
  const t = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
  t.style.cssText = "position:fixed;opacity:0";
  ($("dialog[open]") || document.body).appendChild(t);  // inside an open dialog, or the select is blocked
  t.select();
  try { document.execCommand("copy") ? done() : manual(); } catch { manual(); }
  t.remove();
}
// A one-time password, with Copy. It stays put until the next one for the same place replaces it.
function renderTemp(el, u, pw) {
  el.innerHTML = `<p>Give <b>${esc(u)}</b> this one-time password. It won't be shown again, and they'll pick their own when they sign in.</p>
    <div class="secret">${esc(pw)}</div>
    <div class="row"><button class="plain small" data-copy>Copy</button></div>`;
  el.querySelector("[data-copy]").onclick = () => copyText(pw);
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}
function showTemp(u, pw) { renderTemp($("#nuOut"), u, pw); }

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
  // <details>: opens with a tap, Enter or Space, and screen readers say whether it's open.
  return `<details class="ev"><summary><span class="${e.ok ? "" : "err"}" aria-hidden="true">${e.ok ? "·" : "✗"}</span> ${esc(t)} <b>${esc(e.user)}</b> ${esc(e.kind)}/${esc(e.action)}
    ${e.ok ? "" : `<span class="err">failed${e.error ? ": " + esc(e.error) : ""}</span>`}</summary><pre>${esc(JSON.stringify(extra, null, 2))}</pre></details>`;
}

async function loadActivity(u) {
  $("#activityCard").hidden = false;
  $("#actUser").textContent = u;
  try {
    const evs = await api(`/admin/users/${encodeURIComponent(u)}/activity?limit=100`);
    $("#activity").innerHTML = evs.map(eventRow).join("") || `<p class="muted">No activity yet.</p>`;
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
    const ps = d.printer.printers || [], a = d.auto_print_last || {};
    const day = (iso) => new Date(iso + "T12:00").toLocaleDateString([], { dateStyle: "medium" });
    const rows = [
      ["Printers", ps.length ? ps.map((p) => `${esc(p.name)}: ${p.reachable ? `<span class="ok">online</span>` : `<span class="err">not answering</span>`}` +
          ` · ${esc(p.stock_color)} labels${p.label ? ` · ${esc(p.label)}${/^\d+$/.test(p.label) ? " mm" : ""}` : ""}`).join("<br>") + ` <span class="muted">(${d.printer.ms} ms)</span>`
        : "None in your household yet. Add one under Admin → Printers."],
      ["Your to-do app", d.todoist.ok ? (d.todoist.linked ? `${ok(true)} ${esc(d.todoist.app)} · ${d.todoist.projects} lists · ${d.todoist.ms} ms`
                                                          : `${ok(true)} Only My list (no linked app)`)
                                      : `${ok(false)} ${esc(d.todoist.error)} · ${d.todoist.ms} ms`],
      ["Everyone's apps", Object.entries(d.apps || {}).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ")],
      ["Read-back", `${ok(d.vision.key_set)} ${esc(d.vision.model)}${d.vision.key_set ? "" : " · ANTHROPIC_API_KEY missing"}`],
      ["Errors (24h)", d.counts.errors_24h ? `<span class="err">${d.counts.errors_24h}</span>` : "0"],
      ["Totals", `${d.counts.users} people · ${d.counts.printed} tickets · ${d.counts.scans} scans`],
      ["Auto print", `${a.enabled === false ? "switched off · " : ""}last real print ${a.real ? day(a.real) : "never"} · last preview ${a.dry ? day(a.dry) : "never"}`],
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
  } catch (e) { $("#events").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
$("#evGo").onclick = loadEvents;

// ---------------------------------------------------------------- notes about your own account
// Debug mode and an admin's password reset are never silent: the person sees them.
function showAccountNotes() {
  $("#debugOn").hidden = !me.debug;
  const r = me.password_reset;
  let seen = "";
  try { seen = localStorage.getItem("nb_reset_seen") || ""; } catch {}
  $("#resetNote").hidden = !r || seen === String(r.at);
  if (r) $("#resetText").textContent = `Your password was reset by ${r.by || "an admin"} on ${new Date(r.at * 1000).toLocaleDateString()}. ` +
    "If you didn't expect that, choose a new one in Profile.";
}
$("#resetOk").onclick = () => {
  try { localStorage.setItem("nb_reset_seen", String(me?.password_reset?.at || "")); } catch {}
  $("#resetNote").hidden = true;
};

boot();


// ---------------------------------------------------------------- My list
let listData = { tasks: [], linked: [], tags: [] };
let picked = new Set();
let showingDone = false;
let skipped = new Set();  // "Not now" on this device, until the page reloads
const tagColor = (name) => (listData.tags.find((t) => t.name === name) || {}).color || "white";
const tagChip = (name) => { const c = tagColor(name); return `<span class="tag tc-${esc(c)}${c === "red" ? " solid" : ""}">${esc(name)}</span>`; };
const today = () => new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD, local

function whenText(t) {
  if (!t.due) return "";
  const d = t.due.date.slice(0, 10), now = today();
  const time = t.due.date.length > 10 ? " " + t.due.date.slice(11, 16) : "";
  if (d < now) {
    const days = Math.round((new Date(now) - new Date(d)) / 86400000);
    return `${me?.prefs?.gentle_words === false ? "overdue" : "waiting"} ${days}d`;
  }
  if (d === now) return "today" + time;
  return new Date(d + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + time;
}

function taskRow(t) {
  const steps = t.steps || [];
  const doneSteps = steps.filter((s) => s.done).length;
  const meta = [whenText(t), t.repeat ? "↻ " + t.repeat : "", t.minutes ? `~${t.minutes}m` : "",
                steps.length ? `steps ${doneSteps}/${steps.length}` : "", t.source && t.source !== "nextbox" ? "in " + (listData.linked_name || t.source) : ""]
    .filter(Boolean).map(esc).join(" · ");
  const local = String(t.id).startsWith("nb:");
  return `<div class="task" data-id="${esc(t.id)}">
    ${t.done ? "" : `<input type="checkbox" class="pick" aria-label="Pick for printing" ${picked.has(t.id) ? "checked" : ""}>`}
    <div class="body">
      <div class="text${t.priority >= 3 ? " urgent" : ""}">${(t.labels || []).map(tagChip).join("")}${t.priority >= 3 ? "! " : ""}${esc(t.content)}</div>
      ${meta ? `<div class="meta">${meta}</div>` : ""}
    </div>
    <div class="acts">
      ${t.done ? (local ? `<button class="plain small" data-reopen>Undo</button>` : "") :
        `${local ? `<button class="plain small" data-edit aria-label="Edit">Edit</button>` : ""}<button class="done" data-done aria-label="Done">✓</button>`}
    </div></div>`;
}

function renderList() {
  const now = today();
  const open = listData.tasks.filter((t) => !t.done);
  const groups = [
    ["Today", open.filter((t) => t.due && t.due.date.slice(0, 10) <= now)],
    ["Coming up", open.filter((t) => t.due && t.due.date.slice(0, 10) > now)],
    ["Someday", open.filter((t) => !t.due)],
    [`From ${listData.linked_name || "linked app"} (today)`, listData.linked || []],
    ["Done", showingDone ? listData.tasks.filter((t) => t.done) : []],
  ];
  const html = groups.filter(([, ts]) => ts.length)
    .map(([name, ts]) => `<div class="group">${esc(name)} (${ts.length})</div>${ts.map(taskRow).join("")}`).join("");
  $("#listBody").innerHTML = html || `<p class="muted">Your list is empty. Use the brain dump above to get things out of your head.</p>`;
  $("#listBody").classList.toggle("muted", !html);
  $("#pickCount").textContent = picked.size ? `${picked.size} picked` : "Nothing picked: prints today's tasks";
  renderNow();
}

function renderNow() {
  const open = [...listData.tasks.filter((t) => !t.done), ...(listData.linked || [])];
  let t = listData.one_thing && !skipped.has(listData.one_thing.id) ? listData.one_thing : null;
  if (!t) t = open.find((x) => x.due && x.due.date.slice(0, 10) <= today() && !skipped.has(x.id)) || null;
  $("#nowCard").dataset.id = t ? t.id : "";
  $("#nowText").textContent = t ? t.content : (open.length ? "Nothing else is due today. Pick anything, or rest." : "Nothing due. Nice.");
  const step = t && (t.steps || []).find((s) => !s.done);
  $("#nowStep").textContent = step ? "Next tiny step: " + step.text : (t ? [whenText(t), t.minutes ? `about ${t.minutes} min` : ""].filter(Boolean).join(" · ") : "");
  for (const b of ["#nowDone", "#nowPrint", "#nowSkip"]) $(b).disabled = !t;
}

async function loadList() {
  try { listData = await api("/list" + (showingDone ? "?include_done=true" : "")); }
  catch (e) { $("#listBody").textContent = e.message; return; }
  $("#linkedErr").hidden = !listData.linked_error;
  $("#linkedErr").textContent = listData.linked_error || "";
  const ids = new Set([...listData.tasks, ...(listData.linked || [])].map((t) => t.id));
  picked = new Set([...picked].filter((id) => ids.has(id)));
  renderList();
  setWaiting(listData.marks_waiting || 0);
  if (guide && !guide.steps.find((x) => x.key === "tasks")?.done && listData.tasks.length) loadGuide();
}

$("#listBody").addEventListener("change", (e) => {
  if (!e.target.classList.contains("pick")) return;
  const id = e.target.closest(".task").dataset.id;
  e.target.checked ? picked.add(id) : picked.delete(id);
  renderList();
});
$("#listBody").addEventListener("click", async (e) => {
  const row = e.target.closest(".task");
  if (!row) return;
  const id = row.dataset.id;
  if (e.target.matches("[data-done]")) { await markDone(id); }
  else if (e.target.matches("[data-reopen]")) {
    try { await api(`/list/${encodeURIComponent(id)}/reopen`, { method: "POST" }); loadList(); } catch (err) { listError(err, id); }
  }
  else if (e.target.matches("[data-edit]")) openTask(listData.tasks.find((t) => t.id === id));
});

// A task ticked or deleted on another device: say so, and show the list as it is now.
const TASK_GONE = "That task was already removed.";
function listError(err, id) {
  toast(err.message);
  if (err.message !== TASK_GONE) return;
  if (id) picked.delete(id);
  if ($("#taskDialog").open) $("#taskDialog").close();
  loadList();
}

async function markDone(id) {
  try {
    await api(`/list/${encodeURIComponent(id)}/done`, { method: "POST" });
    picked.delete(id);
    toast(["Done ✓", "Nice. ✓", "That counts. ✓", "One less thing ✓"][Math.floor(Math.random() * 4)]);
    loadList();
  } catch (err) { listError(err, id); }
}

$("#nowDone").onclick = () => $("#nowCard").dataset.id && markDone($("#nowCard").dataset.id);
$("#nowSkip").onclick = () => { skipped.add($("#nowCard").dataset.id); listData.one_thing = null; renderNow(); };
$("#nowPrint").onclick = () => printFrom("/print/focus", { task_id: $("#nowCard").dataset.id }, $("#nowPrint"));

async function printFrom(path, body, button) {
  const dry_run = $("#dry").checked;
  if (!dry_run && !confirm("Send this to the printer?")) return;
  const done = busy(button, dry_run);
  const box = $("#listResult");
  try {
    const r = await api(path, { method: "POST", json: { ...body, dry_run } });
    const gone = (r.missing || []).length;
    box.innerHTML = `<h2 tabindex="-1">${resultHeading(r)}</h2>` +
      (gone ? `<p class="muted">${gone === 1 ? "1 task was" : `${gone} tasks were`} already removed, so ${gone === 1 ? "it isn't" : "they aren't"} on it.</p>` : "") +
      [r, ...(r.also || [])].map(ticketResult).join("<hr>");
    revealResult(box);  // the top of the result, so the preview loading below doesn't push it under the tab bar
    loadGuide();
    if (gone) { for (const id of r.missing) picked.delete(id); loadList(); }
  } catch (e) {
    showResultError(box, errText(e));
    if (/already removed/.test(e.message)) { picked.clear(); loadList(); }
  }
  finally { done(); }
}
$("#printStickers").onclick = () => printFrom("/print/stickers", picked.size ? { task_ids: [...picked] } : {}, $("#printStickers"));
$("#printStrips").onclick = () => printFrom("/print/strips", picked.size ? { task_ids: [...picked] } : {}, $("#printStrips"));
$("#showDone").onclick = () => { showingDone = !showingDone; $("#showDone").textContent = showingDone ? "Hide done" : "Show done"; loadList(); };

$("#dumpGo").onclick = async () => {
  const text = $("#dumpText").value;
  if (!text.trim()) return toast("Nothing to add yet");
  $("#dumpGo").disabled = true;
  try {
    const r = await api("/list/dump", { method: "POST", json: { text } });
    $("#dumpText").value = "";
    $("#dumpMsg").textContent = `Added ${r.added.length}. Your head has more room now.`;
    loadList();
  } catch (e) { $("#dumpMsg").textContent = e.message; }
  finally { $("#dumpGo").disabled = false; }
};

// edit dialog
let editing = null;
function openTask(t) {
  editing = t;
  $("#tdTitle").textContent = "Edit task";
  $("#tdContent").value = t.content;
  $("#tdDate").value = t.due ? t.due.date.slice(0, 10) : "";
  $("#tdTime").value = t.due && t.due.date.length > 10 ? t.due.date.slice(11, 16) : "";
  $("#tdMinutes").value = t.minutes || "";
  $("#tdRepeat").value = t.repeat || "";
  $("#tdPriority").value = t.priority >= 4 ? "4" : t.priority >= 3 ? "3" : "1";
  $("#tdSteps").value = (t.steps || []).map((s) => s.text).join("\n");
  const on = new Set(t.labels || []);
  $("#tdTags").innerHTML = listData.tags.map((g) => `<button type="button" data-tag="${esc(g.name)}" class="${on.has(g.name) ? "on" : ""}">${esc(g.name)}</button>`).join("")
    || `<span class="muted">No tags yet. Add some in Settings.</span>`;
  $("#taskDialog").showModal();
}
$("#tdTags").onclick = (e) => { const b = e.target.closest("[data-tag]"); if (b) b.classList.toggle("on"); };
$("#tdCancel").onclick = () => { editing = null; };
$("#taskForm").onsubmit = async (e) => {
  if (e.submitter?.value !== "save" || !editing) return;
  e.preventDefault();
  const old = new Map((editing.steps || []).map((s) => [s.text, s.done]));
  const body = {
    content: $("#tdContent").value,
    due_date: $("#tdDate").value || null,
    due_time: $("#tdDate").value && $("#tdTime").value ? $("#tdTime").value : null,
    minutes: $("#tdMinutes").value ? Number($("#tdMinutes").value) : null,
    repeat: $("#tdDate").value ? ($("#tdRepeat").value || null) : null,
    priority: Number($("#tdPriority").value),
    tags: $$("#tdTags [data-tag].on").map((b) => b.dataset.tag),
    steps: $("#tdSteps").value.split("\n").map((x) => x.trim()).filter(Boolean).map((text) => ({ text, done: !!old.get(text) })),
  };
  try { await api(`/list/${encodeURIComponent(editing.id)}`, { method: "PATCH", json: body }); $("#taskDialog").close(); toast("Saved"); loadList(); }
  catch (err) { listError(err, editing?.id); }
};
$("#tdDelete").onclick = async () => {
  if (!editing || !confirm(`Delete "${editing.content}"? This can't be undone.`)) return;
  try { await api(`/list/${encodeURIComponent(editing.id)}?confirm=true`, { method: "DELETE" }); $("#taskDialog").close(); toast("Deleted"); loadList(); }
  catch (err) { listError(err, editing?.id); }
};
$("#tdFocus").onclick = () => { const id = editing?.id; $("#taskDialog").close(); if (id) printFrom("/print/focus", { task_id: id }, $("#nowPrint")); };

// tags (Settings)
async function loadTags() {
  let r;
  try { r = await api("/tags"); } catch (e) { $("#tagList").textContent = e.message; return; }
  listData.tags = r.tags;
  const opts = (sel) => r.colors.map((c) => `<option value="${esc(c)}" ${c === sel ? "selected" : ""}>${esc(c)}</option>`).join("");
  $("#newTagColor").innerHTML = opts("yellow");
  $("#tagList").innerHTML = r.tags.map((t) => `<div class="tagrow" data-name="${esc(t.name)}">${tagChip(t.name)}
      <select aria-label="Color for ${esc(t.name)}">${opts(t.color)}</select>
      <button class="plain small" data-del style="margin-left:auto">Remove</button></div>`).join("") || `<p class="muted">No tags.</p>`;
}
$("#tagList").addEventListener("change", async (e) => {
  const row = e.target.closest(".tagrow");
  try { await api(`/tags/${encodeURIComponent(row.dataset.name)}`, { method: "PUT", json: { color: e.target.value } }); toast("Saved"); loadTags(); }
  catch (err) { toast(errText(err)); }
});
$("#tagList").addEventListener("click", async (e) => {
  if (!e.target.matches("[data-del]")) return;
  const name = e.target.closest(".tagrow").dataset.name;
  if (!confirm(`Remove the tag "${name}"? Tasks keep their other tags.`)) return;
  try { await api(`/tags/${encodeURIComponent(name)}`, { method: "DELETE" }); loadTags(); } catch (err) { toast(err.message); }
});
$("#tagForm").onsubmit = async (e) => {  // a form, so Enter adds it too
  e.preventDefault();
  const typed = $("#newTag").value.trim();
  if (!typed) { toast("Type a tag name first"); $("#newTag").focus(); return; }
  try {
    const r = await api("/tags", { method: "POST", json: { name: typed, color: $("#newTagColor").value } });
    $("#newTag").value = "";
    // Tag names are short, lowercase letters, numbers, - and _: say so when what was typed changed.
    toast(r.name === typed.replace(/^[#@]+/, "").toLowerCase() ? `Added #${r.name}` : `Added "${typed}" as #${r.name}`, 3500);
    loadTags();
  } catch (err) { toast(errText(err), 4500); }
};

// ---------------------------------------------------------------- super admin: users
let suRows = [], suPicked = new Set(), suHomes = [], suOpen = null;
const TILES = [["people", "People", ""], ["active_7d", "Active this week", "active"], ["turned_off", "Turned off", "off"],
  ["beta", "Beta testers", "beta"], ["debug", "Debug on", "debug"], ["temp_password", "Temp password", "temp"],
  ["with_errors", "Had errors (7d)", "errors"], ["households", "Households", null], ["prints_30d", "Prints (30d)", null],
  ["open_tasks", "Open tasks", null]];

async function loadSuper() {
  try {
    const [o, homes] = await Promise.all([api("/super/overview"), api("/super/households")]);
    suHomes = homes;
    $("#suTiles").innerHTML = TILES.map(([k, label, f]) =>
      `<button class="tile${f !== null && $("#suStatus").value === (f || "") && f !== "" ? " on" : ""}" ${f === null ? "disabled" : `data-f="${f}"`}><b>${o[k]}</b><span>${esc(label)}</span></button>`).join("");
    const cur = $("#suHome").value;
    $("#suHome").innerHTML = `<option value="">All households</option>` +
      homes.map((h) => `<option value="${esc(h.id)}">${homeName(h)}</option>`).join("");
    $("#suHome").value = homes.some((h) => h.id === cur) ? cur : "";
    renderHomes();
  } catch (e) { $("#suTiles").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  loadSuperList();
}
const homeName = (h) => `${esc(h.name)} (${h.members})`;  // the count tells two similar names apart

// Households: rename any; delete one only when nobody is in it (Home, this Next Box's own, stays).
function renderHomes() {
  $("#suHomesTitle").textContent = `Households (${suHomes.length})`;
  $("#suHomes").innerHTML = suHomes.map((h) => `<div class="mini" data-hid="${esc(h.id)}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span style="flex:1;min-width:0"><b>${esc(h.name)}</b> · ${h.members} ${h.members === 1 ? "person" : "people"}${h.id === "home" ? " · this Next Box's own" : ""}</span>
      <button class="plain small" data-home="rename" aria-label="Rename ${esc(h.name)}">Rename</button>
      ${h.members || h.id === "home" ? "" : `<button class="plain small" data-home="delete" aria-label="Delete ${esc(h.name)}">Delete</button>`}</div>`).join("");
}
$("#suHomes").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-home]"); if (!b) return;
  const h = suHomes.find((x) => x.id === b.closest("[data-hid]").dataset.hid); if (!h) return;
  const path = `/super/households/${encodeURIComponent(h.id)}`;
  try {
    if (b.dataset.home === "rename") {
      const name = prompt(`New name for ${h.name}`, h.name);
      if (name === null || name.trim() === h.name) return;
      await api(path, { method: "PATCH", json: { name } });
      toast("Household renamed");
    } else {
      if (!confirm(`Delete the household "${h.name}"? Nobody is in it. Its invite codes and printers go too.`)) return;
      await api(path, { method: "DELETE" });
      toast("Household deleted");
    }
    loadSuper();
  } catch (err) { toast(errText(err), 4000); }
});

async function loadSuperList() {
  const qs = new URLSearchParams({ q: $("#suQ").value, role: $("#suRole").value, status: $("#suStatus").value,
                                   household: $("#suHome").value });
  try { suRows = await api("/super/users?" + qs); }
  catch (e) { $("#suList").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  const shown = new Set(suRows.map((r) => r.username));
  suPicked = new Set([...suPicked].filter((u) => shown.has(u)));
  $("#suCount").textContent = `${suRows.length} shown`;
  $("#suList").innerHTML = suRows.map((u) => {
    const badges = [`<span class="badge ${u.role !== "user" ? "hot" : ""}">${esc(u.role)}</span>`,
      u.disabled && `<span class="badge bad">off</span>`, u.beta && `<span class="badge">beta</span>`,
      u.debug && `<span class="badge bad">debug</span>`, u.must_change && `<span class="badge">temp password</span>`,
      u.errors_7d && `<span class="badge bad">${u.errors_7d} errors</span>`, !u.consented && `<span class="badge">notice pending</span>`,
      u.protected && `<span class="badge hot">protected</span>`]
      .filter(Boolean).join(" ");
    return `<div class="urow" data-u="${esc(u.username)}">
      <input type="checkbox" aria-label="Select ${esc(u.username)}" ${suPicked.has(u.username) ? "checked" : ""}>
      <div class="who"><div class="top"><b>${esc(u.username)}</b>${badges}</div>
        <div class="muted">${esc(u.email || "no email")} · ${esc(u.household)}</div>
        <div class="muted">last active ${when(lastActive(u))} · ${u.sessions} device(s) · ${u.open_tasks} open tasks · ${u.prints_30d} prints (30d) · ${esc(u.app || "no linked app")}</div></div>
      <button class="plain small" data-manage aria-label="Manage ${esc(u.username)}">Manage</button></div>`;
  }).join("") || `<p class="muted">Nobody matches.</p>`;
  $("#suAll").checked = suRows.length > 0 && suPicked.size === suRows.length;
  renderBulk();
}

function renderBulk() {
  $("#suBulk").hidden = !suPicked.size;
  $("#suPicked").textContent = `${suPicked.size} selected`;
}

let suTimer;
$("#suQ").oninput = () => { clearTimeout(suTimer); suTimer = setTimeout(loadSuperList, 250); };
for (const id of ["#suRole", "#suStatus", "#suHome"]) $(id).onchange = () => { loadSuperList(); if (id === "#suStatus") loadSuper(); };
$("#suTiles").onclick = (e) => {
  const t = e.target.closest("[data-f]");
  if (!t) return;
  $("#suStatus").value = $("#suStatus").value === t.dataset.f ? "" : t.dataset.f;
  loadSuper();
};
$("#suList").addEventListener("change", (e) => {
  if (e.target.type !== "checkbox") return;
  const u = e.target.closest(".urow").dataset.u;
  e.target.checked ? suPicked.add(u) : suPicked.delete(u);
  $("#suAll").checked = suPicked.size === suRows.length;
  renderBulk();
});
$("#suList").addEventListener("click", (e) => { if (e.target.matches("[data-manage]")) openUser(e.target.closest(".urow").dataset.u); });
$("#suAll").onchange = () => { suPicked = $("#suAll").checked ? new Set(suRows.map((r) => r.username)) : new Set(); loadSuperList(); };
$("#suClear").onclick = () => { suPicked.clear(); loadSuperList(); };
$$("[data-bulk]").forEach((b) => b.onclick = async () => {
  const names = [...suPicked];
  if (!confirm(`${b.textContent} ${names.length} ${names.length === 1 ? "person" : "people"}?`)) return;
  try {
    const r = await api("/super/users/bulk", { method: "POST", json: { usernames: names, action: b.dataset.bulk } });
    const all = Object.entries(r.results), odd = all.filter(([, v]) => v !== "done" && !v.startsWith("signed out"));
    const head = `${BULK_DONE[b.dataset.bulk] || "Done:"} ${all.length - odd.length}${odd.length ? `, skipped ${odd.length}` : ""}.`;
    toast(head);
    // Who wasn't changed stays on screen until dismissed: a toast is gone before a list can be read.
    $("#suBulkOut").innerHTML = odd.length ? `<p><b>${esc(head)}</b> Not changed:</p>
      <ul>${odd.map(([u, v]) => `<li><b>${esc(u)}</b>: ${esc(v.replace(/^skipped: /, ""))}</li>`).join("")}</ul>
      <button class="linkish" data-dismiss>Dismiss</button>` : "";
    $("#suBulkOut").hidden = !odd.length;
    suPicked.clear();
    loadSuper();
  } catch (err) { toast(errText(err)); }
});
const BULK_DONE = { signout: "Signed out", disable: "Turned off", enable: "Turned on", beta_on: "Beta on for", beta_off: "Beta off for" };
$("#suBulkOut").onclick = (e) => { if (e.target.matches("[data-dismiss]")) { $("#suBulkOut").innerHTML = ""; $("#suBulkOut").hidden = true; } };

$("#suAdd").onclick = () => {
  $("#suNew").innerHTML = `<div class="field"><label for="snName">Username</label><input type="text" id="snName" autocapitalize="none"></div>
    <div class="field"><label for="snEmail">Email (optional)</label><input type="text" id="snEmail" autocapitalize="none"></div>
    <div class="row"><select id="snRole" aria-label="Role"><option>user</option><option>admin</option><option>superadmin</option></select>
      <select id="snHome" aria-label="Household">${suHomes.map((h) => `<option value="${esc(h.id)}" ${h.id === me.household_id ? "selected" : ""}>${homeName(h)}</option>`).join("")}</select>
      <label class="muted"><input type="checkbox" id="snBeta"> beta tester</label>
      <button class="go" id="snGo">Create</button></div><div id="snOut"></div>`;
  $("#snGo").onclick = async () => {
    try {
      const r = await api("/super/users", { method: "POST", json: { username: $("#snName").value, email: $("#snEmail").value || null,
        role: $("#snRole").value, household_id: $("#snHome").value, beta: $("#snBeta").checked } });
      renderTemp($("#snOut"), r.username, r.temp_password);
      $("#snName").value = $("#snEmail").value = ""; $("#snBeta").checked = false;  // ready for the next person
      loadSuper();
    } catch (e) { toast(errText(e)); }
  };
};
$("#suNewHome").onclick = async () => {
  const name = prompt("Name for the new household (e.g. Chen family)");
  if (!name) return;
  try { await api("/super/households", { method: "POST", json: { name } }); toast("Household created"); loadSuper(); }
  catch (e) { toast(errText(e), 4000); }
};

async function openUser(name) {
  let u;
  try { u = await api(`/super/users/${encodeURIComponent(name)}`); } catch (e) { return toast(e.message); }
  const fresh = !$("#userDialog").open || suOpen?.username !== u.username;  // not just refreshing after an action
  suOpen = u;
  $("#udName").textContent = u.username;
  $("#udSub").textContent = `joined ${when(u.created)} · last active ${when(lastActive(u))} · last sign-in ${when(u.last_login)}${u.consented ? "" : " · hasn't accepted the data notice"}`;
  $("#udEmail").value = u.email || "";
  $("#udRole").value = u.role;
  $("#udHome").innerHTML = suHomes.map((h) => `<option value="${esc(h.id)}">${homeName(h)}</option>`).join("");
  $("#udHome").value = u.household_id;
  $("#udOff").checked = u.disabled; $("#udBeta").checked = u.beta; $("#udDebug").checked = u.debug; $("#udTemp").checked = u.must_change;
  const self = u.username === me.username, locked = self || u.protected;
  $("#udOff").disabled = locked; $("#udRole").disabled = locked;
  // Only the Next Box computer changes the protected admin (the server refuses the rest too).
  const guarded = u.protected && !self;
  for (const id of ["#udReset", "#udSignout", "#udHome", "#udTemp", "#udEmail", "#udPrefs"]) $(id).disabled = guarded;
  // Your own password and devices live in Profile; from here they'd only fail or sign you out.
  if (self) for (const id of ["#udReset", "#udSignout", "#udTemp"]) $(id).disabled = true;
  if (u.protected) $("#udSub").textContent += " · protected admin: always a superadmin. Only the Next Box computer can change it";
  if (self) $("#udSub").textContent += " · This is you: change your password and sign out devices in Profile";
  $("#udMsg").textContent = ""; $("#udConfirm").value = ""; $("#udDelete").disabled = true;
  if (fresh) $("#udTempOut").innerHTML = "";  // a one-time password stays while you keep helping the same person
  $("#udBundle").href = `/super/users/${encodeURIComponent(u.username)}/bundle`;
  $("#udUnlink").disabled = guarded || !u.connection.connected || !!u.connection.legacy;
  $("#udInfo").innerHTML = `
    <div class="kv"><dt>To-do app</dt><dd>${u.connection.connected ? `${esc(u.connection.name)}${u.connection.account ? " · " + esc(u.connection.account) : ""}` : "only their built-in list"}</dd>
    <dt>Open tasks</dt><dd>${u.open_tasks}</dd><dt>Prints (30d)</dt><dd>${u.prints_30d}</dd><dt>Errors (7d)</dt><dd>${u.errors_7d}</dd>
    <dt>Tags</dt><dd>${u.tags.map((t) => esc(t.name)).join(", ") || "none"}</dd></div>
    <h4 style="margin:12px 0 4px">Signed-in devices (${u.devices.length})</h4>
    ${u.devices.map((d) => `<div class="mini">${esc(d.agent || "unknown device")} · ${esc(d.ip || "")} · since ${when(d.created)}</div>`).join("") || "<div class='mini'>None</div>"}
    <h4 style="margin:12px 0 4px">Recent activity</h4>
    ${u.recent.map((e) => `<div class="mini ${e.ok ? "" : "err"}">${when(e.ts)} · ${esc(e.action)}${e.error ? " · " + esc(e.error) : ""}</div>`).join("") || "<div class='mini'>Nothing yet</div>"}
    <h4 style="margin:12px 0 4px">Recent tickets</h4>
    ${u.tickets.map((t) => `<div class="mini"><a href="${png(t.id)}" target="_blank" rel="noopener">#${esc(t.id)}</a> · ${esc(t.kind)} · ${esc((t.created_at || "").replace("T", " "))}${t.dry_run ? " · preview" : ""}</div>`).join("") || "<div class='mini'>None</div>"}`;
  if (!$("#userDialog").open) {
    $("#userDialog").showModal();
    $("#udName").focus();  // not the Email box: on a phone that pops the keyboard over the dialog
  }
}

async function udDo(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); await openUser(suOpen.username); loadSuper(); return r; }
  catch (e) { $("#udMsg").textContent = e.message; toast(e.message); }
}
const udPath = () => `/super/users/${encodeURIComponent(suOpen.username)}`;
$("#udSave").onclick = () => {
  const u = suOpen, body = {};
  const email = $("#udEmail").value.trim();
  if (email !== (u.email || "")) body.email = email;
  if ($("#udRole").value !== u.role) body.role = $("#udRole").value;
  if ($("#udHome").value !== u.household_id) body.household_id = $("#udHome").value;
  for (const [id, k] of [["#udOff", "disabled"], ["#udBeta", "beta"], ["#udDebug", "debug"], ["#udTemp", "must_change"]])
    if ($(id).checked !== u[k]) body[k] = $(id).checked;
  if (!Object.keys(body).length) return toast("Nothing changed");
  if (body.role && !confirm(`Make ${u.username} ${/^[aeiou]/i.test(body.role) ? "an" : "a"} ${body.role}?`)) return;
  if (body.disabled && !confirm(`Turn off ${u.username}? They'll be signed out everywhere.`)) return;
  udDo(() => api(udPath(), { method: "PATCH", json: body }), "Saved");
};
$("#udReset").onclick = async () => {
  if (!confirm(`Reset ${suOpen.username}'s password? They'll be signed out everywhere.`)) return;
  try {
    const r = await api(`/admin/users/${encodeURIComponent(suOpen.username)}/reset-password`, { method: "POST" });
    await openUser(suOpen.username);  // "Must pick a new password" now shows ticked
    renderTemp($("#udTempOut"), r.username, r.temp_password);
    loadSuper();
  } catch (e) { toast(errText(e)); }
};
$("#udSignout").onclick = () => udDo(() => api(`/admin/users/${encodeURIComponent(suOpen.username)}/signout`, { method: "POST" }), "Signed out");
$("#udPrefs").onclick = () => confirm(`Put all of ${suOpen.username}'s settings back to the defaults?`) &&
  udDo(() => api(`${udPath()}/reset-settings`, { method: "POST" }), "Settings reset");
$("#udUnlink").onclick = () => confirm(`Unlink ${suOpen.username}'s to-do app? Their built-in list stays. They can link it again in Settings.`) &&
  udDo(() => api(`${udPath()}/unlink`, { method: "POST" }), "Unlinked");
$("#udConfirm").oninput = () => { $("#udDelete").disabled = $("#udConfirm").value.trim().toLowerCase() !== suOpen?.username || suOpen?.username === me.username || !!suOpen?.protected; };
$("#udDelete").onclick = async () => {
  try { await api(`/admin/users/${encodeURIComponent(suOpen.username)}`, { method: "DELETE" }); toast(`Deleted ${suOpen.username}`);
        $("#userDialog").close(); loadSuper(); }
  catch (e) { $("#udMsg").textContent = e.message; }
};
$("#udClose").onclick = () => $("#userDialog").close();
loaders.superusers = loadSuper;


// ---------------------------------------------------------------- join with an invite
function showJoin(code = "") {
  $("#joinForm").hidden = false;
  $("#loginForm").hidden = true;
  $("#showJoin").textContent = "Already have an account? Sign in";
  if (code) $("#joinCode").value = code;
  ($("#joinCode").value ? $("#joinUser") : $("#joinCode")).focus();
}
$("#showJoin").onclick = () => {
  if ($("#joinForm").hidden) return showJoin(pendingJoin);
  forgetJoin();  // they chose to sign in instead
  $("#joinForm").hidden = true; $("#loginForm").hidden = false; $("#showJoin").textContent = "Have an invite code? Join";
  $("#user").focus();
};
$("#joinForm").onsubmit = async (e) => {
  e.preventDefault();
  $("#joinMsg").textContent = "";
  try {
    await api("/auth/signup", { method: "POST", json: { invite: $("#joinCode").value, username: $("#joinUser").value,
      email: $("#joinEmail").value || null, password: $("#joinPass").value, agree: $("#joinAgree").checked } });
    forgetJoin();
    $("#joinForm").reset();
    resetShow();
    boot();
  } catch (err) { $("#joinMsg").textContent = errText(err); }
};
$("#joinForm").addEventListener("input", () => { $("#joinMsg").textContent = ""; });
$("#joinCode").addEventListener("input", (e) => {  // 817fe370f205 or "817F E370 F205" → 817F-E370-F205
  const raw = e.target.value.toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 12);
  const v = (raw.match(/.{1,4}/g) || []).join("-");
  if (v !== e.target.value) e.target.value = v;
});

// ---------------------------------------------------------------- invites (Admin tab)
async function loadInvites() {
  let list;
  try { list = await api("/admin/invites"); } catch (e) { $("#ivList").textContent = e.message; return; }
  if (me.role === "superadmin") {
    try {
      const homes = await api("/super/households");
      const cur = $("#ivHome").value;
      $("#ivHome").innerHTML = homes.map((h) => `<option value="${esc(h.id)}">join ${homeName(h)}</option>`).join("");
      $("#ivHome").value = homes.some((h) => h.id === cur) ? cur : me.household_id;
    } catch {}
  }
  const row = (i) => `<div class="mini" data-code="${esc(i.code)}"><b>${esc(i.code)}</b>
      · ${esc(i.note || "no note")} · ${i.own_household ? "own household" : "joins " + esc(i.household)}${i.beta ? " · beta" : ""}
      · ${i.status === "used" ? "used by " + esc(i.used_by) : i.status === "expired" ? "expired" : "open until " + when(i.expires)}
      ${i.status === "open" ? `<button class="linkish" data-copylink aria-label="Copy the link for ${esc(i.code)}">copy link</button>
        · <button class="linkish" data-revoke aria-label="Cancel ${esc(i.code)}">cancel</button>` : ""}</div>`;
  // Open codes first; used and expired ones fold away and can be cleared.
  const open = list.filter((i) => i.status === "open"), done = list.filter((i) => i.status !== "open");
  $("#ivList").innerHTML = (open.map(row).join("") || (done.length ? "No open invites." : "None yet.")) +
    (done.length ? `<details style="margin-top:6px"><summary>Older invites (${done.length})</summary>${done.map(row).join("")}
      <div class="row"><button class="linkish" id="ivClear">Clear used and expired</button></div></details>` : "");
}
$("#ivOwn").onchange = () => { $("#ivHome").disabled = $("#ivOwn").checked; };
$("#ivGo").onclick = async () => {
  try {
    const r = await api("/admin/invites", { method: "POST", json: { note: $("#ivNote").value, beta: $("#ivBeta").checked,
      own_household: $("#ivOwn").checked, days: Number($("#ivDays").value),
      household_id: me.role === "superadmin" ? $("#ivHome").value : null } });
    const link = location.origin + r.link;
    $("#ivOut").innerHTML = `<p>Send them this code or link. It works once.</p><div class="secret">${esc(r.code)}</div>
      <div class="muted" style="word-break:break-all">${esc(link)}</div>
      ${["localhost", "127.0.0.1", "::1"].includes(location.hostname) ? `<p class="err">That link only works on this computer, because you opened Next Box as "${esc(location.hostname)}". Send the code instead, or open Next Box at the phone address that <code>nextbox doctor</code> shows and make the invite there.</p>` : ""}
      <div class="row"><button class="plain small" id="ivCopy">Copy link</button></div>`;
    $("#ivCopy").onclick = () => copyText(link);
    $("#ivNote").value = "";
    loadInvites();
  } catch (e) { toast(e.message); }
};
$("#ivList").addEventListener("click", async (e) => {
  if (e.target.id === "ivClear") {
    try { const r = await api("/admin/invites/clear", { method: "POST" }); toast(r.removed ? `Cleared ${r.removed} old invite${r.removed === 1 ? "" : "s"}` : "Nothing to clear"); loadInvites(); }
    catch (err) { toast(errText(err)); }
    return;
  }
  const code = e.target.closest("[data-code]")?.dataset.code;
  if (e.target.matches("[data-copylink]")) return copyText(`${location.origin}/app/#join=${code}`);
  if (!e.target.matches("[data-revoke]")) return;
  if (!confirm(`Cancel invite ${code}?`)) return;
  try { await api(`/admin/invites/${encodeURIComponent(code)}`, { method: "DELETE" }); toast("Invite cancelled"); }
  catch (err) { toast(errText(err)); }
  loadInvites();  // either way: a code that was just used or ran out shows as such
});


// ---------------------------------------------------------------- calendars
let calKinds = [];
async function loadCalendars() {
  let r;
  try { r = await api("/calendars"); } catch (e) { $("#calList").textContent = e.message; return; }
  calKinds = r.kinds;
  $("#calList").innerHTML = r.calendars.map((c) => `<div class="tagrow" data-cal="${esc(c.id)}" data-kind="${esc(c.provider)}" style="flex-wrap:wrap">
      <div style="flex:1;min-width:0"><b class="calname">${esc(c.name)}</b><div class="muted">${esc((calKinds.find((k) => k.key === c.provider) || {}).name || c.provider)}${c.calendars?.length ? " · " + c.calendars.map(esc).join(", ") : ""}</div></div>
      <button class="plain small" data-rename aria-label="Rename ${esc(c.name)}">Rename</button>
      <button class="plain small" data-remove>Remove</button>
      <div class="calstatus" style="flex-basis:100%"></div></div>`).join("") || `<p class="muted">No calendars yet. Add one to see today's events here and on your daily ticket.</p>`;
  // Which ones aren't working right now (otherwise that only shows on My list).
  if (r.calendars.length) api("/calendars/today").then((t) => {
    for (const [id, msg] of Object.entries(t.failing || {})) {
      const row = $(`#calList [data-cal="${CSS.escape(id)}"]`);
      if (!row) continue;
      const fix = row.dataset.kind === "ics" ? "Remove it, then add it again with a fresh link."
        : row.dataset.kind === "caldav" ? "Remove it, then add it again with a new app password." : "Remove it, then sign in again.";
      row.querySelector(".calstatus").innerHTML = `<div class="err">Not working: ${esc(msg)}</div>${/again/i.test(msg) ? "" : `<div class="muted">${fix}</div>`}`;
    }
  }).catch(() => {});
  const usable = calKinds.filter((k) => k.status === "ready" || k.status === "oauth");
  const cur = $("#calKind").value;
  $("#calKind").innerHTML = usable.map((k) => `<option value="${esc(k.key)}">${esc(k.name)}</option>`).join("");
  if (cur && usable.some((k) => k.key === cur)) $("#calKind").value = cur;
  $("#calOther").innerHTML = calKinds.filter((k) => k.also).map((k) => `<p><b>${esc(k.name)}</b> covers ${esc(k.also)}.</p>`).join("") +
    calKinds.filter((k) => k.status === "needs_setup").map((k) => `<p><b>${esc(k.name)}</b>: not switched on yet. ${esc(k.reason)}</p>`).join("");
  renderCalFields();
}
function renderCalFields() {
  const k = calKinds.find((x) => x.key === $("#calKind").value);
  if (!k) return;
  $("#calGo").textContent = k.status === "oauth" ? `Sign in with ${k.app === "google" ? "Google" : "Microsoft"}` : "Add calendar";
  // A link is shown as typed (it's kept sealed all the same); only a password gets dots, with Show.
  const input = (f) => {
    const id = `cf_${esc(f.name)}`, attrs = `id="${id}" data-cfield="${esc(f.name)}" autocapitalize="none" spellcheck="false"`;
    if (f.name === "url") return `<input ${attrs} type="url" autocomplete="off" placeholder="https://">`;
    if (f.secret) return `<span class="pwrow"><input ${attrs} type="password" autocomplete="new-password"><button type="button" data-show="${id}">Show</button></span>`;
    return `<input ${attrs} type="text" autocomplete="off">`;
  };
  $("#calFields").innerHTML = k.status === "oauth" ? `<p class="muted">You'll sign in on ${esc(k.name)}'s own page and come straight back. Next Box only reads events.</p>` :
    (k.fields || []).map((f) => `<div class="field"><label for="cf_${esc(f.name)}">${esc(f.label)}${f.optional ? " (optional)" : ""}</label>
      ${input(f)}
      ${f.help ? `<div class="muted">${esc(f.help)}</div>` : ""}</div>`).join("");
  $("#calMsg").textContent = "";
}
$("#calKind").onchange = renderCalFields;
$("#calGo").onclick = async () => {
  const k = calKinds.find((x) => x.key === $("#calKind").value);
  if (k?.status === "oauth") { location.href = `/connect/${encodeURIComponent(k.app)}/calendar/start`; return; }
  const fields = {};
  $$("#calFields [data-cfield]").forEach((el) => fields[el.dataset.cfield] = el.value);
  $("#calGo").disabled = true; $("#calMsg").textContent = "Checking…"; $("#calMsg").className = "muted";
  try {
    await api("/calendars", { method: "POST", json: { kind: k.key, fields } });
    $$("#calFields input").forEach((el) => el.value = "");
    $("#calMsg").textContent = ""; toast("Calendar added"); loadCalendars(); loadToday(); loadGuide();
  } catch (e) { $("#calMsg").textContent = errText(e); $("#calMsg").className = "err"; }
  finally { $("#calGo").disabled = false; }
};
// Rename in place: the name turns into a box with Save and Cancel.
$("#calList").addEventListener("click", (e) => {
  if (!e.target.matches("[data-rename]")) return;
  const row = e.target.closest("[data-cal]"), name = row.querySelector(".calname");
  e.target.hidden = true;
  name.outerHTML = `<form class="row calrename" style="margin:0 0 4px"><input type="text" maxlength="60" value="${esc(name.textContent)}" aria-label="Calendar name" style="flex:1;min-width:0">
    <button class="go small" type="submit">Save</button><button class="plain small" type="button" data-cancel>Cancel</button></form>`;
  const form = row.querySelector(".calrename"), box = form.querySelector("input");
  box.focus(); box.select();
  form.querySelector("[data-cancel]").onclick = () => loadCalendars();
  box.onkeydown = (ev) => { if (ev.key === "Escape") { ev.preventDefault(); loadCalendars(); } };
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    try { await api(`/calendars/${encodeURIComponent(row.dataset.cal)}`, { method: "PATCH", json: { name: box.value } }); toast("Renamed"); loadCalendars(); loadToday(); }
    catch (err) { toast(errText(err), 4000); box.focus(); }
  };
});
$("#calList").addEventListener("click", async (e) => {
  if (!e.target.matches("[data-remove]")) return;
  const id = e.target.closest("[data-cal]").dataset.cal;
  if (!confirm("Remove this calendar from Next Box? The calendar itself isn't changed.")) return;
  try { await api(`/calendars/${encodeURIComponent(id)}`, { method: "DELETE" }); loadCalendars(); loadToday(); } catch (err) { toast(err.message); }
});

async function loadToday() {
  let r;
  try { r = await api("/calendars/today"); } catch { $("#todayCard").hidden = true; return; }
  const clock = (hhmm) => { const h = +hhmm.slice(0, 2); return me?.prefs?.time_24h ? hhmm : `${h % 12 || 12}:${hhmm.slice(3, 5)}${h < 12 ? "a" : "p"}`; };
  $("#todayCard").hidden = !r.events.length && !r.problems.length;
  $("#todayEvents").innerHTML = r.events.map((e) => `<div class="mini"><b style="display:inline-block;min-width:70px">${e.all_day ? "all day" : esc(clock(e.start))}</b>${esc(e.title)}${e.location ? ` <span class="muted">· ${esc(e.location)}</span>` : ""}</div>`).join("")
    + r.problems.map((p) => `<div class="mini err">${esc(p)}</div>`).join("");
}


// ---------------------------------------------------------------- Get started guide
const isApp = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
const isAndroid = () => /Android/.test(navigator.userAgent);
let installPrompt = null;  // Chrome and Edge's own "Install app", when they offer it (https only)
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installPrompt = e; renderGuide(); });
window.addEventListener("appinstalled", () => {
  installPrompt = null;
  if (me) api("/setup/home/done", { method: "POST" }).then(loadGuide).catch(() => {});
});
async function install() {
  const p = installPrompt;
  installPrompt = null;
  p.prompt();
  try { if ((await p.userChoice).outcome === "accepted") await api("/setup/home/done", { method: "POST" }); } catch {}
  loadGuide();
}
function appNames() {  // only the apps this Next Box offers right now
  const names = providers.filter((p) => p.status === "ready" || p.status === "oauth")
    .sort((a, b) => (a.key === "caldav") - (b.key === "caldav")).map((p) => (p.key === "caldav" ? "a CalDAV app" : p.name));
  if (!names.length) return "Todoist, Notion, Asana or a CalDAV app";
  return names.length === 1 ? names[0] : names.slice(0, -1).join(", ") + " or " + names[names.length - 1];
}
function goto(tab, selector) {
  $(`#topnav button[data-tab=${tab}]`)?.click();
  setTimeout(() => { const el = selector && $(selector); if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.querySelector?.("textarea,input,select")?.focus?.(); } }, 150);
}
let guide = null;
const STEPS = {
  tasks: { title: "Empty your head", text: "Type everything on your mind into Brain dump, one thing per line. Don't sort it.",
           go: ["Go to Brain dump", () => { $("#dumpText").scrollIntoView({ behavior: "smooth", block: "center" }); $("#dumpText").focus(); }] },
  home: { title: () => (isIOS() || isAndroid() ? "Put Next Box on your home screen" : "Install Next Box as an app"),
          text: () => isIOS() ? "In Safari tap Share (the square with the arrow) → Add to Home Screen. Then open Next Box from that icon."
                    : isAndroid() ? "In Chrome tap ⋮ → Add to Home screen (or Install app). Then open Next Box from that icon."
                    : "In Chrome or Edge, click the install icon at the right of the address bar. No icon? A bookmark works too.",
          go: () => (installPrompt ? ["Install", install] : null),
          mark: "I did it" },
  app: { title: "Link your to-do app (optional)", text: () => `Already use ${appNames()}? Its tasks show up here too.`,
         go: ["Link an app", () => goto("settings", "#appCard")], skip: true },
  calendar: { title: "Add your calendar (optional)", text: "Today's events show here and print at the top of your daily ticket. Google, Outlook and iCloud all work.",
              go: ["Add a calendar", () => goto("settings", "#calCard")], skip: true },
  printer: { title: () => guide?.can_add_printer ? "Add your label printer" : "Your printer",
             text: () => guide?.can_add_printer ? "Admin → Printers for a label printer. No label printer? Every ticket has Print on paper, which works with any printer."
                                                : "No label printer? Every ticket has Print on paper, which works with any printer. A household admin can add a label printer later.",
             go: () => guide?.can_add_printer ? ["Add printer", () => goto("admin", "#printerList")] : null, skip: true },
  preview: { title: "Make your first stickers", text: "Tick a task or two and tap Stickers. With “Preview only” on, nothing prints: you just see how it looks.",
             go: ["Show me", () => $(".printbar").scrollIntoView({ behavior: "smooth", block: "center" })] },
  today: { title: "Print today's ticket", text: "Today's tasks on one label, with the next tiny step under each. Tap Print today. With “Preview only” on, you just see it.",
           go: ["Show me", () => { goto("print"); setTimeout(() => $("[data-act=today]").focus(), 200); }] },
  snap: { title: "Mark it and snap it", text: "Tick a row with a pen, then take a photo in Snap. Try it on a preview: print it on paper or hold the preview up to your camera.",
          go: ["Open Snap", () => goto("scan")], skip: true },
  focus: { title: "Make it fit your brain", text: "How many tasks per day, gentle wording, yesterday's wins, tiny steps: it's all in Focus & ADHD helpers.",
           go: ["Open", () => goto("settings", "#focusCard")], mark: "Looks good" },
};
const val = (v) => (typeof v === "function" ? v() : v);

async function loadGuide() {
  try { guide = await api("/setup"); } catch { return; }
  const home = guide.steps.find((s) => s.key === "home");
  if (home && !home.done && isApp()) { await api("/setup/home/done", { method: "POST" }).catch(() => {}); home.done = true; }
  renderGuide();
}
function renderGuide() {
  if (!guide) return;
  $("#guideCard").hidden = guide.hidden;
  const done = guide.steps.filter((s) => s.done).length, total = guide.steps.length;
  $("#guideBar").style.width = `${Math.round(100 * done / total)}%`;
  $("#guideCount").textContent = done === total ? "You're all set. 🎉 You can hide this now." : `${done} of ${total} done`;
  const next = guide.steps.find((s) => !s.done);
  // Once there's a list, the checklist shrinks to one line so "Just one thing" stays on top.
  const open = guideOpen();
  $("#guideHead").hidden = $("#guideFull").hidden = !open;
  $("#guideMini").hidden = open;
  $("#guideLess").hidden = !open || !guide.steps.find((s) => s.key === "tasks")?.done;
  $("#guideMini").textContent = next ? `Get started: ${done} of ${total} · Next: ${val(STEPS[next.key].title)} ›` : "Get started: you're all set 🎉 ›";
  $("#guideSteps").innerHTML = guide.steps.map((s, i) => {
    const d = STEPS[s.key], go = val(d.go);
    const isNext = next && s.key === next.key;
    const skipped = s.skipped && d.skip;  // "I did it" steps are done, not skipped
    return `<div class="step${s.done ? " done" : ""}${isNext ? " next" : ""}" data-step="${s.key}">
      <div class="mark">${s.done ? "✓" : i + 1}</div>
      <div style="flex:1;min-width:0"><div class="title">${esc(val(d.title))}${skipped ? " <span class='muted'>(skipped)</span>" : ""}</div>
        ${isNext ? `<div class="muted">${esc(val(d.text))}</div>
          <div class="row">${go ? `<button class="go small" data-go>${esc(go[0])}</button>` : ""}
            ${d.mark ? `<button class="plain small" data-done>${esc(d.mark)}</button>` : ""}
            ${d.skip ? `<button class="plain small" data-done>${s.key === "printer" && !guide.can_add_printer ? "OK" : "Skip for now"}</button>` : ""}</div>` : ""}
        ${skipped ? `<button class="linkish" data-undo>undo skip</button>` : ""}</div></div>`;
  }).join("");
}
$("#guideSteps").addEventListener("click", async (e) => {
  const row = e.target.closest("[data-step]");
  if (!row) return;
  const key = row.dataset.step;
  if (e.target.matches("[data-go]")) return val(STEPS[key].go)[1]();
  const action = e.target.matches("[data-done]") ? "done" : e.target.matches("[data-undo]") ? "undo" : null;
  if (!action) return;
  try { await api(`/setup/${key}/${action}`, { method: "POST" }); loadGuide(); } catch (err) { toast(err.message); }
});
function guideOpen() {
  if (!guide.steps.find((s) => s.key === "tasks")?.done) return true;  // brand new: show every step
  try { return localStorage.getItem("nb_guide_open") === "1"; } catch { return false; }
}
function setGuideOpen(open) { try { localStorage.setItem("nb_guide_open", open ? "1" : "0"); } catch {} renderGuide(); }
$("#guideMini").onclick = () => setGuideOpen(true);
$("#guideLess").onclick = () => setGuideOpen(false);
$("#guideHide").onclick = async () => {
  await api("/setup/guide/hide", { method: "POST" }).catch(() => {});
  $("#guideCard").hidden = true;
  toast("Hidden. Bring it back any time: Settings → Help.");
};
$("#guideShow").onclick = async () => {
  await api("/setup/guide/show", { method: "POST" }).catch(() => {});
  try { localStorage.setItem("nb_guide_open", "1"); } catch {}
  goto("mylist", "#guideCard");
  loadGuide();
};


// ---------------------------------------------------------------- journal, wins, slugs
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-go-tab]");
  if (t) goto(t.dataset.goTab);
});
// "Today" and "Yesterday" come from the server (`d`), whose clock files the pages, not this device's.
const dayName = (iso, d = {}) => d.is_today ? "Today" : d.is_yesterday ? "Yesterday"
  : new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
let jUntil = "", jToday = "";
const jData = new Map();                  // day -> that day as drawn, so a page can move between days in place
const jRange = { newest: "", oldest: "" };  // the days the list covers now (empty ones included)
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + n); return d.toLocaleDateString("en-CA"); };
const dayFlags = (iso) => ({ is_today: iso === jToday, is_yesterday: !!jToday && iso === addDays(jToday, -1) });
const NO_JOURNAL =`<div class="card"><p class="muted">Nothing here yet. Print today's label, paste it in your journal,
      write on the right page tonight, then Snap both pages.</p></div>`;

// `from`: show that day and older (a search result was opened), with "Back to today" above.
async function loadJournal(more = false, from = "") {
  await flushJournal();  // an edit still being typed is saved before the pages are drawn again
  if (!more) {
    jUntil = from; jData.clear(); $("#jDays").innerHTML = `<p class="muted">Loading…</p>`; $("#jEnd").hidden = true;
    $("#jBack").hidden = true;
    if (!from) { loadWins(); loadSlugs(); }
  }
  try {
    const r = await api(`/journal?days=14${jUntil ? "&until=" + jUntil : ""}`);
    jToday = r.today;
    const asked = jUntil || r.today;
    r.days.forEach((d) => jData.set(d.day, d));
    if (!more) jRange.newest = asked;
    jRange.oldest = addDays(asked, -13);
    const html = r.days.map((d) => dayCard(d)).join("");
    if (!more) $("#jDays").innerHTML = html || NO_JOURNAL;
    else $("#jDays").insertAdjacentHTML("beforeend", html);
    if (from && from !== r.today) {
      $("#jBackText").textContent = `Showing ${dayName(from, dayFlags(from))} and earlier.`;
      $("#jBack").hidden = false;
    }
    jUntil = r.next_until;
    $("#jOlder").hidden = !r.has_more;
    $("#jEnd").hidden = !(more && !r.has_more);
    wireJournal();
  } catch (e) { $("#jDays").innerHTML = `<p class="err">${esc(errText(e))}</p>`; }
}
$("#jOlder").onclick = () => loadJournal(true);

function winsHtml(w) {
  if (!w.count) return "";
  const named = new Map();
  w.items.forEach((t) => named.set(t, (named.get(t) || 0) + 1));
  const items = [...named].map(([t, n]) => `<li>${esc(t)}${n > 1 ? ` ×${n}` : ""}</li>`).join("");
  const linked = w.linked ? `<li class="muted">${items ? "+" : ""}${w.linked} ${items ? "more " : ""}in your linked app</li>` : "";
  return `<div data-wins><p><b>Wins: ${w.count} ✓</b></p><ul class="winlist">${items}${linked}</ul></div>`;
}

function pageHtml(p, d) {
  const made = new Date(p.created * 1000);
  const local = `${made.getFullYear()}-${String(made.getMonth() + 1).padStart(2, "0")}-${String(made.getDate()).padStart(2, "0")}`;
  const time = made.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const when = local === d.day ? time : `written ${made.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
  const id = esc(p.id);
  return `<div class="jpage" data-page="${id}" data-photo="${p.has_photo ? "1" : ""}">
      ${p.has_photo ? `<button class="jphoto" data-jphoto aria-label="Open the photo of this page"><img loading="lazy" src="/journal/${id}/photo" alt=""></button>` : ""}
      <label class="sr-only" for="jn-${id}">Notes for ${esc(dayName(d.day, d))}, ${esc(when)}</label>
      <textarea id="jn-${id}" placeholder="${p.has_photo ? "No handwritten notes were found. Type some if you like." : ""}">${esc(p.notes)}</textarea>
      <div class="jacts"><span class="muted jstate" data-jstate>${esc(when)}</span>
        <button class="plain small" data-jlines${p.notes.trim() ? "" : " hidden"}>Add lines to my list</button>
        <button class="plain small" data-jmove aria-expanded="false" aria-controls="jm-${id}">Move to another day</button>
        <button class="plain small warn" data-jdel>${p.has_photo ? "Delete page" : "Delete note"}</button></div>
      <div class="jmove" id="jm-${id}" hidden>
        <label for="jmd-${id}">Move this ${p.has_photo ? "page" : "note"} to</label>
        <input type="date" id="jmd-${id}" value="${esc(d.day)}" max="${esc(jToday)}">
        <button class="go small" data-jmove-go>Move</button><button class="plain small" data-jmove-cancel>Cancel</button>
        <span class="err" data-jmove-msg role="alert"></span></div></div>`;
}

// `moved`: a page just moved from this day to another, with Undo.
function dayCard(d, moved = null) {
  const written = d.pages.filter((p) => p.win).map((p) => `<p class="jwin"><b>Your win:</b> ${esc(p.win)}</p>`).join("");
  const name = dayName(d.day, d);
  const print = d.pages.length || d.wins.count
    ? `<a class="btnlink plain small" href="/journal/export?format=pdf&amp;day=${esc(d.day)}" target="_blank" rel="noopener"
         aria-label="Print my journal page for ${esc(name)}">Print my journal page</a>` : "";
  const note = moved ? `<p class="jmoved" tabindex="-1">Moved a page to ${esc(dayName(moved.to, dayFlags(moved.to)))}.
      <button class="linkish" data-jundo="${esc(moved.id)}" data-from="${esc(moved.from)}" data-to="${esc(moved.to)}">Undo</button></p>` : "";
  return `<div class="card jday" data-day="${esc(d.day)}"><div class="jhead"><h3 tabindex="-1">${esc(name)}</h3>${print}</div>
    ${note}${ticketHtml(d)}${written}${winsHtml(d.wins)}${changesHtml(d.changes)}${d.pages.map((p) => pageHtml(p, d)).join("")}</div>`;
}

// What read-back changed that day (the finished ones are the day's wins, above).
function changesHtml(c) {
  if (!c) return "";
  return [["Moved", c.moved], ["Dropped", c.dropped]].filter(([, names]) => names?.length)
    .map(([what, names]) => `<p class="jchanges">${what}: ${esc(names.join(", "))}</p>`).join("");
}

// That day's ticket: small, with Print on paper. Tap it to see all of it.
function ticketHtml(d) {
  const t = d.ticket;
  if (!t) return "";
  const what = t.kind === "today" ? "Daily ticket" : KIND_NAMES[t.kind] || "Ticket";
  const size = me?.prefs?.paper_size || "letter";
  return `<div class="jticket"><button type="button" data-jticket="${esc(t.id)}" aria-label="Open ${esc(what.toLowerCase())} #${esc(t.id)}">
      <img src="${png(t.id)}" alt="" loading="lazy"></button>
    <div><span class="muted">${esc(what)} #${esc(t.id)}${t.dry_run ? " (preview)" : ""}</span>
      <a class="btnlink plain small" href="/printed/${encodeURIComponent(t.id)}/pdf?paper=${esc(size)}" target="_blank" rel="noopener"
         aria-label="Print ticket #${esc(t.id)} on paper">Print on paper</a></div></div>`;
}

function fitText(t) { t.style.height = "auto"; t.style.height = (t.scrollHeight + 4) + "px"; }

// Notes save themselves a moment after you stop typing.
const jPending = new Map();  // page id -> { timer, textarea, state }
async function saveNote(id, t, state) {
  jPending.delete(id);
  try {
    await api(`/journal/${id}`, { method: "PATCH", json: { notes: t.value }, keepalive: true });
    state.textContent = "Saved ✓"; state.className = "muted";
  } catch (e) { state.textContent = errText(e); state.className = "err"; }
}
function flushJournal() {
  const all = [...jPending.entries()];
  all.forEach(([, x]) => clearTimeout(x.timer));
  return Promise.all(all.map(([id, x]) => saveNote(id, x.t, x.state)));
}
window.addEventListener("pagehide", () => { flushJournal(); });

function wireJournal() {
  $$("#jDays textarea").forEach((t) => {
    fitText(t);  // show the whole page of notes
    t.oninput = () => {
      fitText(t);
      const page = t.closest("[data-page]"), id = page.dataset.page, state = page.querySelector("[data-jstate]");
      clearTimeout(jPending.get(id)?.timer);
      state.textContent = "Saving…"; state.className = "muted";
      jPending.set(id, { timer: setTimeout(() => saveNote(id, t, state), 800), t, state });
    };
  });
  $$("#jDays [data-jdel]").forEach((b) => b.onclick = async () => {
    const page = b.closest("[data-page]"), id = page.dataset.page;
    if (!confirm(page.dataset.photo ? "Delete this page and its photo? This can't be undone." : "Delete this note? This can't be undone.")) return;
    clearTimeout(jPending.get(id)?.timer); jPending.delete(id);
    try {
      await api(`/journal/${id}?confirm=true`, { method: "DELETE" });
      const day = page.closest(".jday"), kept = jData.get(day?.dataset.day);
      if (kept) kept.pages = kept.pages.filter((p) => p.id !== id);
      page.remove();
      if (day && !day.querySelector(".jpage, [data-wins], .jwin, .jchanges")) day.remove();
      if (!$("#jDays .jday")) $("#jDays").innerHTML = NO_JOURNAL;
      toast("Deleted");
    } catch (e) { toast(errText(e)); }
  });
}
$("#jDays").addEventListener("click", (e) => {  // a photo opens here, so its address stays out of the history
  const b = e.target.closest("[data-jphoto]");
  if (!b) return;
  $("#photoBig").src = b.querySelector("img").src;
  $("#photoDialog").showModal();
});
$("#photoClose").onclick = () => $("#photoDialog").close();
$("#photoDialog").addEventListener("close", () => $("#photoBig").removeAttribute("src"));

async function loadWins() {
  try {
    const r = await api("/journal/wins?days=7");
    const max = Math.max(1, ...r.series.map((x) => x.count));
    const wd = (x) => new Date(x.day + "T12:00:00").toLocaleDateString(undefined, { weekday: "short" });
    $("#winsChart").innerHTML = r.series.map((x) => `<div class="${x.is_today ? "today" : ""}" aria-hidden="true"><i style="height:${Math.round(x.count / max * 52)}px"></i>${x.count || ""}<span>${esc(wd(x))}</span></div>`).join("");
    $("#winsChart").setAttribute("aria-label", `Wins this week: ${r.series.map((x) => `${wd(x)}${x.is_today ? " (today)" : ""} ${x.count}`).join(", ")}`);
    $("#winsTotal").textContent = r.total ? `${r.total} ${r.total === 1 ? "thing" : "things"} done this week. That counts.` : "Your wins this week will show here.";
  } catch { $("#winsChart").innerHTML = ""; }
}

async function loadSlugs() {
  try {
    const r = await api("/journal/slugs");
    $("#slugCard").hidden = !r.slugs.length;
    $("#slugList").innerHTML = r.slugs.map((s) => `<div class="slug" data-task="${esc(s.id)}" data-content="${esc(s.content)}">
        <div><b>${esc(s.content)}</b><div class="muted">${s.moves ? `moved ${s.moves}×` : ""}${s.moves && s.waiting_days ? " · " : ""}${s.waiting_days ? `waiting ${s.waiting_days}d` : ""}</div></div>
        <div class="row" style="margin:0">${s.id.startsWith("nb:") ? `<button class="plain small" data-slug-steps>Make it smaller</button>` : ""}
          <button class="plain small" data-slug-go>Let it go</button><button class="linkish small" data-slug-not>Not a slug</button>
          <button class="plain small" data-slug-focus>Print just this</button></div></div>`).join("");
    $$("#slugList [data-slug-focus]").forEach((b) => b.onclick = async () => {
      const id = b.closest("[data-task]").dataset.task;
      applyDry();  // honours paused printing, preview-only and "no printer yet"
      const dry_run = $("#dry").checked;
      if (!dry_run && !confirm("Send this to the printer?")) return;
      const box = $("#slugResult"), done = busy(b, dry_run);
      try {
        const t = await api("/print/focus", { method: "POST", json: { task_id: id, dry_run } });
        box.innerHTML = `<h2 tabindex="-1">${resultHeading(t)}</h2>` + ticketResult(t);
        revealResult(box);
      } catch (e) { showResultError(box, e.message); }
      finally { done(); }
    });
    $$("#slugList [data-slug-steps]").forEach((b) => b.onclick = () => makeSmaller(b.closest("[data-task]").dataset.task));
    $$("#slugList [data-slug-go]").forEach((b) => b.onclick = async () => {
      const s = b.closest("[data-task]");
      if (!confirm(`Let go of "${s.dataset.content}"? It will be deleted.`)) return;
      try { await api(`/list/${encodeURIComponent(s.dataset.task)}?confirm=true`, { method: "DELETE" }); dropSlug(s, "Let go. One less thing."); }
      catch (e) { toast(errText(e)); }
    });
    $$("#slugList [data-slug-not]").forEach((b) => b.onclick = async () => {
      const s = b.closest("[data-task]");
      try { await api(`/journal/slugs/${encodeURIComponent(s.dataset.task)}`, { method: "DELETE" }); dropSlug(s, "Okay. It's off this list."); }
      catch (e) { toast(errText(e)); }
    });
  } catch { $("#slugCard").hidden = true; }
}
function dropSlug(el, msg) {
  el.remove();
  if (!$("#slugList .slug")) $("#slugCard").hidden = true;
  toast(msg);
}

// "Make it smaller": the task's tiny steps, right here.
const STEPS_HINT = $("#tdSteps").placeholder;
async function makeSmaller(id) {
  if (!listData.tasks.some((t) => t.id === id)) await loadList();
  const t = listData.tasks.find((x) => x.id === id);
  if (!t) return toast(TASK_GONE);
  openTask(t);
  $("#tdSteps").placeholder = "What's a 2-minute first step?";
  $("#tdSteps").focus();
}
$("#taskDialog").addEventListener("close", () => {
  $("#tdSteps").placeholder = STEPS_HINT;
  if ($("#journal").classList.contains("on")) loadSlugs();
});

$("#jWrite").onclick = () => {
  // Today by this Next Box's clock; just after midnight, the note is probably about last night.
  const today = jToday || new Date().toISOString().slice(0, 10);
  const d = new Date(today + "T12:00:00");
  if (new Date().getHours() < 4) d.setDate(d.getDate() - 1);
  $("#jDay").max = today;
  $("#jDay").value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  $("#jNew").hidden = false; $("#jText").focus();
};
$("#jCancel").onclick = () => { $("#jNew").hidden = true; };
$("#jSave").onclick = async () => {
  if (!$("#jText").value.trim()) return toast("Write something first");
  try {
    await api("/journal", { method: "POST", json: { notes: $("#jText").value, day: $("#jDay").value || null } });
    $("#jText").value = ""; $("#jNew").hidden = true; toast("Saved to your journal"); loadJournal();
  } catch (e) { toast(errText(e)); }
};

// ---------------------------------------------------------------- journal: move a page to another day
// The page moves in place: out of its day (which says "Moved … Undo"), into the new day if it's on screen.
const dayHasMore = (d) => d.pages.length || d.wins.count || d.wins.items.length || d.changes;
function drawDay(day, moved = null) {
  const d = jData.get(day), old = $(`#jDays .jday[data-day="${CSS.escape(day)}"]`);
  if (!d || (!dayHasMore(d) && !moved)) {
    old?.remove();
    if (!$("#jDays .jday")) $("#jDays").innerHTML = NO_JOURNAL;
    return null;
  }
  const box = document.createElement("div");
  box.innerHTML = dayCard(d, moved);
  const card = box.firstElementChild;
  if (old) old.replaceWith(card);
  else {
    $("#jDays > .card:not(.jday)")?.remove();  // "Nothing here yet"
    const after = $$("#jDays .jday").find((c) => c.dataset.day < day);  // newest first
    after ? after.before(card) : $("#jDays").append(card);
  }
  return card;
}
function keepTyping(day) {  // what's typed on screen is what's drawn again
  const d = jData.get(day);
  $$(`#jDays .jday[data-day="${CSS.escape(day)}"] [data-page]`).forEach((el) => {
    const p = d?.pages.find((x) => x.id === el.dataset.page);
    if (p) p.notes = el.querySelector("textarea").value;
  });
}
async function movePage(id, from, to, undoing = false) {
  await flushJournal();
  const page = await api(`/journal/${id}`, { method: "PATCH", json: { day: to } });
  keepTyping(from);
  const src = jData.get(from);
  const moving = { ...(src?.pages.find((p) => p.id === id) || {}), ...page };
  if (src) src.pages = src.pages.filter((p) => p.id !== id);
  if (to >= jRange.oldest && to <= jRange.newest) {  // on screen: show it there too
    keepTyping(to);
    const dst = jData.get(to) || { day: to, pages: [], wins: { count: 0, items: [], linked: 0 }, ticket: null, changes: null, ...dayFlags(to) };
    dst.pages = [...dst.pages.filter((p) => p.id !== id), moving].sort((a, b) => a.created - b.created);
    jData.set(to, dst);
    drawDay(to);
  }
  const card = drawDay(from, undoing ? null : { id, from, to });
  wireJournal();
  return card;
}
$("#jDays").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const page = b.closest("[data-page]"), form = page?.querySelector(".jmove");
  if (b.matches("[data-jmove]")) {
    form.hidden = !form.hidden;
    b.setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.querySelector("input").focus();
  } else if (b.matches("[data-jmove-cancel]")) {
    form.hidden = true;
    page.querySelector("[data-jmove]").setAttribute("aria-expanded", "false");
    page.querySelector("[data-jmove]").focus();
  } else if (b.matches("[data-jmove-go]")) {
    const from = page.closest(".jday").dataset.day, to = form.querySelector("input").value, msg = form.querySelector("[data-jmove-msg]");
    msg.textContent = !to ? "Pick a day." : to > jToday ? "That day hasn't happened yet." : to === from ? "It's already on that day." : "";
    if (msg.textContent) return;
    b.disabled = true;
    try {
      const card = await movePage(page.dataset.page, from, to);
      card?.querySelector(".jmoved")?.focus();
    } catch (err) { msg.textContent = errText(err); b.disabled = false; }
  } else if (b.matches("[data-jundo]")) {
    b.disabled = true;
    try {
      await movePage(b.dataset.jundo, b.dataset.to, b.dataset.from, true);
      $(`#jDays [data-page="${CSS.escape(b.dataset.jundo)}"] [data-jmove]`)?.focus();
      toast("Moved back");
    } catch (err) { b.disabled = false; toast(errText(err)); }
  } else if (b.matches("[data-jlines]")) {
    openLines(page.querySelector("textarea").value, b);
  } else if (b.matches("[data-jticket]")) {
    $("#photoBig").src = b.querySelector("img").src;
    $("#photoBig").alt = `Ticket #${b.dataset.jticket}`;
    $("#photoDialog").setAttribute("aria-label", "Ticket");
    $("#photoDialog").showModal();
  }
});
$("#jDays").addEventListener("keydown", (e) => {  // Enter in the date box moves it
  if (e.key === "Enter" && e.target.matches(".jmove input")) { e.preventDefault(); e.target.closest(".jmove").querySelector("[data-jmove-go]").click(); }
});
$("#jDays").addEventListener("input", (e) => {  // "Add lines to my list" once a page has notes
  const page = e.target.matches("textarea") && e.target.closest("[data-page]");
  if (page) page.querySelector("[data-jlines]").hidden = !e.target.value.trim();
});
$("#jDays").addEventListener("error", (e) => {  // a ticket image that's gone (kept 90 days): no empty box
  if (e.target.matches?.(".jticket img")) e.target.closest("button").hidden = true;
}, true);
$("#photoDialog").addEventListener("close", () => {
  $("#photoDialog").setAttribute("aria-label", "Journal page photo"); $("#photoBig").alt = "Your journal page";
});

// ---------------------------------------------------------------- journal: search
let jSearchTimer = null, jSearchN = 0;
function searching(on) {
  $("#journal").classList.toggle("searching", on);
  $("#jFound").hidden = !on;
}
function endSearch(clear = true) {
  clearTimeout(jSearchTimer); jSearchN++;
  if (clear) $("#jQuery").value = "";
  searching(false);
  $("#jFoundList").innerHTML = ""; $("#jSearchStatus").textContent = "";
}
const longDay = (iso) => {
  const f = dayFlags(iso);
  if (f.is_today || f.is_yesterday) return dayName(iso, f);
  return new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric",
    ...(iso.slice(0, 4) !== (jToday || today()).slice(0, 4) ? { year: "numeric" } : {}) });
};
const hitHtml = (h) => `<button class="jhit" data-hit-day="${esc(h.day)}" data-hit-page="${esc(h.id)}">
    <span class="when"><b>${esc(longDay(h.day))}</b> · ${esc(new Date(h.created * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}${h.has_photo ? " · photo" : ""}</span>
    <span class="snip">${h.parts.map((p) => p.m ? `<mark>${esc(p.t)}</mark>` : esc(p.t)).join("")}</span></button>`;
async function runSearch() {
  clearTimeout(jSearchTimer);
  const q = $("#jQuery").value.trim().replace(/\s+/g, " ");
  if (!q) return endSearch(false);
  searching(true);
  const n = ++jSearchN, head = $("#jFoundHead");
  if (q.length < 2) { head.textContent = "Type at least 2 letters."; $("#jFoundList").innerHTML = ""; return; }
  head.textContent = "Searching…";
  try {
    const r = await api(`/journal/search?q=${encodeURIComponent(q)}`);
    if (n !== jSearchN) return;  // they kept typing: a newer search is on its way
    const said = r.total ? `${r.total} ${r.total === 1 ? "page matches" : "pages match"} “${q}”`
      + (r.total > r.results.length ? `. Here are the newest ${r.results.length}.` : "")
      : `Nothing in your journal matches “${q}”.`;
    head.textContent = said;
    $("#jSearchStatus").textContent = said;
    $("#jFoundList").innerHTML = r.results.map(hitHtml).join("")
      || `<p class="muted">Check the spelling, or try fewer words.</p>`;
  } catch (e) { if (n === jSearchN) { head.textContent = errText(e); $("#jFoundList").innerHTML = ""; } }
}
$("#jQuery").addEventListener("input", () => { clearTimeout(jSearchTimer); jSearchTimer = setTimeout(runSearch, 350); });
$("#jQuery").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runSearch(); }
  if (e.key === "Escape" && $("#jQuery").value) { e.preventDefault(); endSearch(); }
});
$("#jFoundClear").onclick = () => { endSearch(); $("#jQuery").focus(); };
$("#jFoundList").onclick = async (e) => {  // open the page: that day and older, the page outlined
  const b = e.target.closest("[data-hit-day]");
  if (!b) return;
  const { hitDay, hitPage } = b.dataset;
  endSearch(false);
  await loadJournal(false, hitDay);
  const page = $(`#jDays [data-page="${CSS.escape(hitPage)}"]`);
  if (!page) return;
  page.classList.add("jfound");
  page.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
  page.closest(".jday").querySelector("h3").focus({ preventScroll: true });
};
$("#jBackGo").onclick = () => { loadJournal(); window.scrollTo(0, 0); };

// ---------------------------------------------------------------- journal: notes -> tasks
// "Add lines to my list" on a journal page or a Snap result: the lines that look like to-dos are
// ticked, each with what Brain dump will make of it. Only the ticked lines are sent to the list.
let lines = { items: [], opener: null };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
function lineChips(x) {
  const t = x.task || {}, c = [];
  if (x.state === "added") c.push(["ok", "added ✓"]);
  if (x.on_list) c.push(["", "already on your list"]);
  if (x.done) c.push(["", "looks done"]);
  if (x.unclear) c.push(["warn", "a word couldn't be read: edit it first"]);
  if (t.problem) c.push(["warn", t.problem]);
  if (t.due_date) c.push(["", "📅 " + whenText({ due: { date: t.due_date + (t.due_time ? "T" + t.due_time : "") } })]);
  if (t.repeat) c.push(["", "↻ " + t.repeat]);
  if (t.minutes) c.push(["", `~${t.minutes}m`]);
  if (t.priority >= 3) c.push(["", "! urgent"]);
  return c.map(([k, s]) => `<span class="pchip ${k}">${esc(s)}</span>`).join("") + (t.tags || []).map(tagChip).join("");
}
function lineHtml(x, i) {
  const shown = x.task?.content || x.text, added = x.state === "added";
  const body = x.editing
    ? `<input type="text" data-ledit-input value="${esc(x.text)}" aria-label="Line ${i + 1}" maxlength="500">`
    : `<label class="ltext" for="ln-${i}">${esc(shown)}</label>`;
  const chips = x.editing ? "" : lineChips(x);
  const btn = added ? "" : x.editing ? `<button class="linkish" data-ldone>Done</button>`
    : `<button class="linkish" data-ledit aria-label="Edit ${esc(shown)}">Edit</button>`;
  return `<li data-i="${i}" class="${added ? "added" : ""}"><div class="lrow">
      <input type="checkbox" id="ln-${i}" ${x.pick ? "checked" : ""} ${added ? "disabled" : ""} ${x.editing ? `aria-label="Add line ${i + 1}"` : ""}>
      <div class="lbody">${body}${chips ? `<div class="pchips">${chips}</div>` : ""}</div>${btn}</div></li>`;
}
function drawLines(focus = null) {
  if (!lines.items.length) {
    $("#linesBody").innerHTML = `<p class="muted">No lines to add. Put each to-do on its own line, like “- call the dentist”.</p>`;
  } else {
    $("#linesBody").innerHTML = `<div class="row lall" style="margin:0"><button class="linkish" data-lines-all>Tick all</button>
        <button class="linkish" data-lines-none>Untick all</button></div><ul class="lines">${lines.items.map(lineHtml).join("")}</ul>`;
  }
  countLines();
  if (focus) $(`#linesBody li[data-i="${focus.i}"] ${focus.sel}`)?.focus();
}
function countLines() {
  const n = lines.items.filter((x) => x.pick && x.state !== "added").length;
  $("#linesAdd").disabled = !n;
  $("#linesAddN").textContent = n ? `Add ${n}` : "Add";
}
async function openLines(text, opener) {
  lines = { items: [], opener };
  $("#linesMsg").textContent = ""; $("#linesMsg").className = "";
  $("#linesAdd").disabled = true; $("#linesAddN").textContent = "Add";
  if (!$("#linesDialog").open) $("#linesDialog").showModal();
  if (!text.trim()) { $("#linesBody").innerHTML = `<p class="muted">This page has no notes yet.</p>`; return; }
  $("#linesBody").innerHTML = `<p class="muted"><span class="spin" aria-hidden="true"></span>Reading the lines…</p>`;
  try {
    const r = await api("/journal/lines", { method: "POST", json: { text } });
    lines.items = r.lines.map((x) => ({ ...x, pick: x.suggest, state: "" }));
    drawLines();
    $("#linesBody input[type=checkbox]")?.focus();  // straight to the first line, not Close
  } catch (e) {
    $("#linesBody").innerHTML = `<p class="err">${esc(errText(e))}</p><div class="row"><button class="plain small" data-lines-retry>Try again</button></div>`;
    $("#linesBody [data-lines-retry]").onclick = () => openLines(text, opener);
  }
}
async function finishEdit(i) {
  const x = lines.items[i], input = $(`#linesBody li[data-i="${i}"] [data-ledit-input]`);
  const text = (input?.value || "").trim();
  x.editing = false;
  if (text && text !== x.text) {
    x.text = text; x.pick = true;  // they fixed it so it can go on the list
    try {
      const [fresh] = (await api("/journal/lines", { method: "POST", json: { text: "- " + text } })).lines;
      if (fresh) Object.assign(x, { text: fresh.text, task: fresh.task, unclear: fresh.unclear, on_list: fresh.on_list, done: false });
    } catch { x.task = null; }
  }
  drawLines({ i, sel: "[data-ledit]" });
}
$("#linesBody").addEventListener("change", (e) => {
  const li = e.target.closest("li[data-i]");
  if (!li || e.target.type !== "checkbox") return;
  lines.items[li.dataset.i].pick = e.target.checked;
  countLines();
});
$("#linesBody").addEventListener("click", (e) => {
  const b = e.target.closest("button"), li = b?.closest("li[data-i]");
  if (!b) return;
  if (b.matches("[data-lines-all], [data-lines-none]")) {
    const on = b.matches("[data-lines-all]");
    lines.items.forEach((x, i) => {
      if (x.state === "added") return;
      x.pick = on;
      const box = $(`#ln-${i}`);
      if (box) box.checked = on;
    });
    countLines();
  } else if (b.matches("[data-ledit]")) {
    lines.items[li.dataset.i].editing = true;
    drawLines({ i: li.dataset.i, sel: "[data-ledit-input]" });
  } else if (b.matches("[data-ldone]")) finishEdit(Number(li.dataset.i));
});
$("#linesBody").addEventListener("keydown", (e) => {
  if (!e.target.matches("[data-ledit-input]")) return;
  const i = Number(e.target.closest("li").dataset.i);
  if (e.key === "Enter") { e.preventDefault(); finishEdit(i); }
  if (e.key === "Escape") { e.preventDefault(); lines.items[i].editing = false; drawLines({ i, sel: "[data-ledit]" }); }
});
$("#linesAdd").onclick = async () => {
  for (const [i, x] of lines.items.entries()) if (x.editing) await finishEdit(i);  // what's typed counts
  const chosen = lines.items.filter((x) => x.pick && x.state !== "added");
  if (!chosen.length) return;
  const msg = $("#linesMsg");
  $("#linesAdd").disabled = true; msg.className = "muted"; msg.textContent = "Adding…";
  try {
    const r = await api("/list/dump", { method: "POST", json: { text: chosen.map((x) => x.text).join("\n") } });
    chosen.forEach((x) => { x.state = "added"; x.pick = false; });
    drawLines();
    msg.className = "ok";
    msg.innerHTML = `Added ${esc(plural(r.added?.length ?? chosen.length, "task"))} to ${esc(r.to || "your list")}. ✓
      <button class="linkish" data-go-tab="mylist">Open My list</button>`;
    loadList();
  } catch (e) {
    msg.className = "err";
    msg.textContent = e.status && typeof e.message === "string" && e.message !== "[object Object]" ? e.message
      : e.status ? "Some lines couldn't be added. Check them and try again." : OFFLINE;
    drawLines();
  }
};
$("#linesClose").onclick = () => $("#linesDialog").close();
$("#linesDialog").addEventListener("click", (e) => { if (e.target.closest("[data-go-tab]")) $("#linesDialog").close(); });
$("#linesDialog").addEventListener("close", () => { if (lines.opener?.isConnected) lines.opener.focus(); });

// ---------------------------------------------------------------- download my journal
// From the Journal tab, Profile and the delete-account panel. The summary says what's in it (and
// whether a download is allowed right now) before the browser downloads the file itself.
let jxN = 0;
function jxParams() {
  const p = new URLSearchParams({ format: document.querySelector("input[name=jxFormat]:checked").value });
  if ($("#jxRange").value) p.set("since", addDays(jToday || today(), -(Number($("#jxRange").value) - 1)));
  if (p.get("format") === "pdf") p.set("paper", $("#jxPaper").value);
  return p;
}
const shortDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
async function jxSummary() {
  const n = ++jxN, p = jxParams(), go = $("#jxGo"), out = $("#jxSummary"), pdf = p.get("format") === "pdf";
  $("#jxPaperField").hidden = !pdf;
  go.textContent = pdf ? "Download PDF" : "Download .zip";
  go.removeAttribute("href"); go.setAttribute("aria-disabled", "true");
  out.className = "muted"; out.textContent = "Looking at your journal…";
  try {
    const s = await api("/journal/export/summary" + (p.get("since") ? `?since=${p.get("since")}` : ""));
    if (n !== jxN) return;
    if (!s.days) {
      out.textContent = p.get("since") ? "Nothing in your journal for these days yet." : "Nothing in your journal yet. Snap a page or write a note first.";
      return;
    }
    if (!s.can_download) { out.className = "err"; out.textContent = "That's a lot of downloads this hour. Try again in a bit."; return; }
    const range = s.first === s.last ? shortDate(s.first) : `${shortDate(s.first)} to ${shortDate(s.last)}`;
    const bits = [plural(s.days, "day"), plural(s.photos, "photo"), s.wins ? plural(s.wins, "win") : ""].filter(Boolean).join(", ");
    const size = !pdf && s.bytes > 1_000_000 ? ` About ${Math.round(s.bytes / 1_000_000)} MB.` : "";
    out.textContent = `${range}: ${bits}.${size}`;
    go.href = `/journal/export?${p}`; go.removeAttribute("aria-disabled");
  } catch (e) { if (n === jxN) { out.className = "err"; out.textContent = errText(e); } }
}
function openJournalExport() {
  $("#jxPaper").value = me?.prefs?.paper_size || "letter";
  if (!$("#jxDialog").open) $("#jxDialog").showModal();
  jxSummary();
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-journal-export]")) { e.preventDefault(); openJournalExport(); }
});
$("#jxDialog").addEventListener("change", jxSummary);
$("#jxGo").addEventListener("click", (e) => {
  if (!$("#jxGo").href) return e.preventDefault();
  $("#jxSummary").className = "muted";
  $("#jxSummary").textContent = "Your download is starting. A big journal can take a moment to put together.";
});
$("#jxClose").onclick = () => $("#jxDialog").close();
closeOnBackdrop($("#jxDialog"));


// ---------------------------------------------------------------- print on paper (any printer)
const PAPERS = [["letter", "Letter"], ["a4", "A4"], ["a5", "A5"], ["half_letter", "Half letter"]];
function paperButton(id) {
  const size = me?.prefs?.paper_size || "letter";
  return `<div class="row paperrow"><a class="go small" style="text-decoration:none" data-paper-link href="/printed/${encodeURIComponent(id)}/pdf?paper=${size}" target="_blank" rel="noopener">Print on paper</a>
    <select data-paper aria-label="Paper size">${PAPERS.map(([v, n]) => `<option value="${v}"${v === size ? " selected" : ""}>${n}</option>`).join("")}</select>
    <span class="muted">Any printer: opens a page, then use your phone's or computer's Print.</span></div>`;
}
document.addEventListener("change", (e) => {
  if (!e.target.matches?.("select[data-paper]")) return;
  const link = e.target.closest(".paperrow").querySelector("[data-paper-link]");
  link.href = link.href.replace(/paper=[a-z_]+/, `paper=${e.target.value}`);
});


// ---------------------------------------------------------------- your account (top right)
document.addEventListener("click", (e) => {  // delegated, so Show also works on fields drawn later (calendars)
  const b = e.target.closest("[data-show]"), input = b && $("#" + b.dataset.show);
  if (!input) return;
  input.type = input.type === "password" ? "text" : "password";
  b.textContent = input.type === "password" ? "Show" : "Hide";
});
function resetShow() {  // passwords go back to dots once they've been used
  $$("[data-show]").forEach((b) => { const input = $("#" + b.dataset.show); if (input) input.type = "password"; b.textContent = "Show"; });
}
let update = null;
async function checkUpdate() {
  try { update = await api("/super/update-check"); } catch { return; }
  $("#updateBanner").hidden = !update.update_available;
  $("#updateBanner").textContent = update.update_available
    ? `Next Box ${update.latest} is available (you have ${update.current}). ${update.how}` : "";
}
function openMe() {
  $("#meAvatar").textContent = initial();
  $("#meName").textContent = displayName();
  $("#meSub").textContent = [me.role === "user" ? "" : me.role, me.email || "no email yet", me.protected ? "protected admin" : ""].filter(Boolean).join(" · ");
  $("#meUpdate").textContent = `Next Box ${me.version}` + (update ? (update.update_available ? ` · update available: ${update.latest}` : update.latest ? " · up to date ✓" : "") : "");
  $("#mePanel").innerHTML = "";
  $("#meDialog [data-me=delete]").hidden = me.can_delete === false;  // the protected admin or the only superadmin
  $("#meDialog").showModal();
}
$("#meBtn").onclick = openMe;
$("#meClose").onclick = () => $("#meDialog").close();
// A tap on the dimmed area closes a menu, like on most phones. The rectangle check keeps taps on
// the sheet's own padding from closing it. Never for the data notice or a task being edited.
function closeOnBackdrop(dlg, ok = () => true) {
  dlg.addEventListener("click", (e) => {
    if (e.target !== dlg || !ok()) return;
    const r = dlg.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
  });
}
closeOnBackdrop($("#meDialog"));
closeOnBackdrop($("#moreDialog"));
closeOnBackdrop($("#fbDialog"), () => !$("#fbDone").hidden || !$$("#fbDialog textarea").some((t) => t.value.trim()));  // nothing typed is lost
// What deleting your account removes, and how to keep a copy first.
const DELETE_WARNING = `<p class="err">This deletes your account, your list, tags, journal pages and journal photos, tickets and history from Next Box. It can't be undone. Apps you linked (like Todoist) aren't touched.</p>
  <p class="muted">Want a copy first? <a href="/auth/export?format=zip">Download my data</a>: one .zip file with your list, settings, journal pages and photos, and ticket images.</p>
  <p class="muted"><button class="linkish" data-journal-export>Download your journal first</button>: a folder for each day, or a PDF you can print.</p>`;
$("#meDialog").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-me]");
  if (!b) return;
  const what = b.dataset.me, panel = $("#mePanel");
  if (what === "settings") { $("#meDialog").close(); return goto("settings"); }
  if (what === "profile") { $("#meDialog").close(); return goto("profile"); }
  if (what === "feedback") { $("#meDialog").close(); return document.querySelector("[data-feedback]")?.click(); }
  if (what === "refresh") return location.reload();
  if (what === "export") { location.href = "/auth/export?format=zip"; return; }
  if (what === "signout") return signOut(panel);
  if (what === "delete") {
    panel.innerHTML = `${DELETE_WARNING}
      <div class="field"><label for="meDelPw">Your password</label><input type="password" id="meDelPw" autocomplete="current-password"></div>
      <div class="field"><label for="meDelName">Type your username (${esc(me.username)})</label><input type="text" id="meDelName" autocapitalize="none"></div>
      <div class="row"><button class="go warn small" id="meDelGo">Delete my account forever</button><span class="err" id="meMsg"></span></div>`;
    $("#meDelGo").onclick = async () => {
      try { await api("/auth/delete-account", { method: "POST", json: { password: $("#meDelPw").value, confirm: $("#meDelName").value } });
            startOver("Your account was deleted."); }
      catch (err) { $("#meMsg").textContent = err.message; }
    };
  }
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});


// ---------------------------------------------------------------- profile page and look
const displayName = () => (me?.prefs?.display_name || "").trim() || me?.username || "";
const initial = () => {  // the first letter as people see it: a whole emoji or accented letter, never half of one
  const n = displayName();
  const seg = typeof Intl.Segmenter === "function" ? [...new Intl.Segmenter().segment(n)][0]?.segment : Array.from(n)[0];
  return (seg || "?").toUpperCase();
};
function applyLook() {
  if (!me) return;
  $("#meBtn").textContent = initial();
  const size = me.prefs.text_size || "normal";
  if (size === "normal") delete document.documentElement.dataset.text; else document.documentElement.dataset.text = size;
}
function loadProfile() {
  if (!me) return;
  $("#pAvatar").textContent = initial();
  $("#pName").textContent = displayName();
  $("#pDisplay").value = me.prefs.display_name || "";
  $("#pEmail").value = me.email || "";
  const facts = [["Email", me.email || "not set"], ["Household", me.household || me.household_id],
                 ["Member since", me.created ? new Date(me.created * 1000).toLocaleDateString() : ""],
                 ["Last sign-in", when(me.last_login)], ["App version", `Next Box ${me.version}`]];
  if (me.protected) facts.push(["Account", "protected admin (always a superadmin)"]);
  $("#pFacts").innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  // The protected admin and the only superadmin can't be deleted: say so up front, not after the password.
  $("#pDelete").disabled = me.can_delete === false;
  $("#pDeletePanel").innerHTML = me.can_delete === false ? `<p class="muted">${me.protected
    ? "This is the protected admin account, so it can't be deleted. It always stays a superadmin."
    : "You're the only superadmin. Make someone else a superadmin first."}</p>` : "";
  loadSessions();
}
// Name and email are saved together: both or neither, and the page shows what was saved.
$("#pForm").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("#pMsg");
  msg.textContent = ""; msg.className = "muted";
  if (!$("#pEmail").checkValidity()) {
    msg.textContent = "That doesn't look like an email address."; msg.className = "err"; $("#pEmail").focus(); return;
  }
  try {
    const r = await api("/auth/profile", { method: "PATCH", json: { display_name: $("#pDisplay").value.trim(), email: $("#pEmail").value.trim() } });
    me.prefs = r.prefs; me.email = r.email;
    applyLook(); loadProfile(); toast("Saved");
  } catch (err) { msg.textContent = errText(err); msg.className = "err"; }
};
$("#pForm").addEventListener("input", () => { $("#pMsg").textContent = ""; });
$("#pDelete").onclick = () => {
  $("#pDeletePanel").innerHTML = `${DELETE_WARNING}
    <div class="field"><label for="pDelPw">Your password</label><input type="password" id="pDelPw" autocomplete="current-password"></div>
    <div class="field"><label for="pDelName">Type your username (${esc(me.username)})</label><input type="text" id="pDelName" autocapitalize="none"></div>
    <div class="row"><button class="go warn" id="pDelGo">Delete my account forever</button><span class="err" id="pDelMsg"></span></div>`;
  $("#pDelGo").onclick = async () => {
    try { await api("/auth/delete-account", { method: "POST", json: { password: $("#pDelPw").value, confirm: $("#pDelName").value } });
          startOver("Your account was deleted."); }
    catch (err) { $("#pDelMsg").textContent = err.message; }
  };
};
$("#setJump").onclick = (e) => {
  const b = e.target.closest("[data-jump]"), card = b && document.getElementById(b.dataset.jump);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  const h = card.querySelector("h2");  // keyboard and screen readers carry on inside the card
  if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
};


// ---------------------------------------------------------------- phone navigation, offline
$$(".bottombar [data-go]").forEach((b) => b.onclick = () => $(`#topnav button[data-tab=${b.dataset.go}]`).click());
$("#moreBtn").onclick = () => {
  if (!me) return;
  const tabs = $$("#topnav button").filter((b) => !b.hidden && !["mylist", "print", "scan", "journal"].includes(b.dataset.tab));
  const icons = { printed: "🧾", board: "💬", profile: "👤", settings: "⚙️", admin: "🛠️", superusers: "👥", usage: "📊", debug: "🐞" };
  $("#moreMenu").innerHTML = tabs.map((b) => `<button data-more="${b.dataset.tab}">${icons[b.dataset.tab] || "•"} ${esc(b.textContent)}</button>`).join("");
  $("#moreDialog").showModal();
};
$("#moreMenu").onclick = (e) => {
  const b = e.target.closest("[data-more]");
  if (!b) return;
  $("#moreDialog").close();
  $(`#topnav button[data-tab=${b.dataset.more}]`).click();
};
$("#moreClose").onclick = () => $("#moreDialog").close();
const setOnline = () => { $("#offlineBanner").hidden = navigator.onLine; };
window.addEventListener("online", setOnline);
window.addEventListener("offline", setOnline);
setOnline();
window.addEventListener("unhandledrejection", (e) => {
  const msg = String(e.reason?.message || e.reason || "");
  toast(msg.includes("fetch") || msg.includes("NetworkError") ? "Can't reach Next Box. Check your connection." : "Something went wrong. Try again.");
});
