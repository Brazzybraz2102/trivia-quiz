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
  $("#fb").hidden = !me.beta;
  $("#announce").hidden = !me.announcement;
  $("#announce").textContent = me.announcement || "";
  $("#paused").hidden = !me.printing_paused;
  $$("[data-min]").forEach((el) => el.hidden = RANK[me.role] < RANK[el.dataset.min]);
  $$("#nuRole option[data-min]").forEach((o) => o.disabled = RANK[me.role] < RANK[o.dataset.min]);
  loadPrefs(me.prefs);
  refreshStatus();
}

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
const loaders = { printed: loadHistory, settings: loadSessions, admin: loadAdmin, debug: loadDebug };
$$("nav button").forEach((b) => b.onclick = () => {
  $$("nav button").forEach((x) => x.classList.toggle("on", x === b));
  $$("section").forEach((s) => s.classList.toggle("on", s.id === b.dataset.tab));
  loaders[b.dataset.tab]?.();
});

async function refreshStatus() {
  try {
    const s = await api("/status");
    $("#status").textContent = `printer ${s.printer_reachable ? "online" : "offline"}${s.dry_run_forced ? " · preview only" : ""}`;
  } catch (e) { $("#status").textContent = `server: ${e.message}`; }
}

// ---------------------------------------------------------------- print
function showPrint(r) {
  const box = $("#printResult");
  box.hidden = false;
  if (r.status === "skipped") { box.innerHTML = `<b>Skipped</b> ${esc(r.reason)}`; return; }
  box.innerHTML = `<b>${r.status === "printed" ? "Printed" : "Preview"}</b> #${esc(r.id)} · ${r.manifest.length} rows
    <img src="${png(r.id)}" alt="ticket preview">`;
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
  catch (e) { $("#printResult").hidden = false; $("#printResult").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
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

async function sendFeedback(text) {
  if (!text.trim()) return toast("Write something first");
  await api("/feedback", { method: "POST", json: { message: text, include_debug: $("#fbDebug").checked,
    page: document.querySelector("nav button.on")?.dataset.tab || "" } });
  toast("Thanks! Sent to the admins.");
}
$("#fbSend").onclick = async () => { try { await sendFeedback($("#fbText").value); $("#fbText").value = ""; } catch (e) { toast(e.message); } };
$("#fb").onclick = async () => {
  const text = prompt("What happened? (Your recent errors are attached.)");
  if (text) try { await sendFeedback(text); } catch (e) { toast(e.message); }
};

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
        <div class="muted">last sign-in ${when(u.last_login)} · ${u.sessions} device(s) · ${u.prints} prints · ${u.errors_7d} errors this week</div>
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
  loadFeedback();
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

async function loadFeedback() {
  try {
    const items = await api("/admin/feedback?limit=50");
    $("#feedback").innerHTML = items.map((f) => `<div class="user"><div class="top"><b>${esc(f.user)}</b>
      <span class="muted">${when(f.ts)}${f.detail.page ? " · on " + esc(f.detail.page) : ""}</span></div>
      <div>${esc(f.detail.message)}</div>
      ${(f.detail.recent_errors || []).length ? `<div class="muted">recent errors: ${f.detail.recent_errors.map((x) => esc(x.action + ": " + x.error)).join("; ")}</div>` : ""}</div>`).join("")
      || `<p class="muted">No feedback yet.</p>`;
  } catch (e) { $("#feedback").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
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
      ["Todoist", `${ok(d.todoist.ok)} ${d.todoist.ok ? d.todoist.projects + " projects" : esc(d.todoist.error)} · ${d.todoist.ms} ms`],
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
