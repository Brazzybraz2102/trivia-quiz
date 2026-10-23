const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const RANK = { user: 0, admin: 1, superadmin: 2 };
let me = null;

let lastFailure = { at: 0, msg: "" };  // the last request that failed, so toast() can tell an error from news
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  let r;
  try { r = await fetch(path, { ...opts, headers, credentials: "same-origin" }); }
  catch {  // no answer at all (offline, server down): never the browser's "Failed to fetch"
    lastFailure = { at: Date.now(), msg: OFFLINE };
    throw new Error(OFFLINE);
  }
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith("/auth/")) { showLogin("Your session ended. Sign in again."); }
  if (!r.ok) {
    const msg = body.detail || ([502, 503, 504].includes(r.status) ? BUSY : `${r.status}`);
    lastFailure = { at: Date.now(), msg };
    throw Object.assign(new Error(msg), { status: r.status, body });
  }
  return body;
}
const OFFLINE = "Can't reach Next Box. Check your connection.";
const BUSY = "Next Box didn't answer in time. Try again in a moment.";
const errText = (e) => (e.status ? e.message : OFFLINE);  // a fetch that never got an answer has no status
const png = (id) => `/printed/${encodeURIComponent(id)}/png`;  // a ticket never changes once made
// Next Box can only reach printers at its own computer's home. A household with no printer of its own there
// (server: prints_here false) prints from its own device for now. The same words as jobs.PRINT_AWAY.
const PRINT_AWAY = "In this test version, labels print straight to a printer only at the Next Box computer's home. "
  + "To print yours, tap Print on paper, or Save label image and open it in your label printer's app (like Brother iPrint&Label).";
const printsHere = () => me?.prints_here !== false;  // Home, or a household with a printer: the messages as they always were
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "never";

// A short message at the bottom. It stays long enough to read (4 s or more for longer ones), and it's
// announced to screen readers. An error (a request that just failed, or { error: true }) stays 10 s, has
// a × to close it, and waits while the pointer or focus is on it. The second argument can also be the
// least number of ms to show it, as older callers pass.
let toastTimer, toastLeft = 0, toastStarted = 0;
function toast(msg, opts = {}) {
  if (typeof opts === "number") opts = { ms: opts };
  msg = String(msg ?? "");
  const t = $("#toast"), text = $("#toastText");
  const error = opts.error ?? (msg === OFFLINE || (msg === lastFailure.msg && Date.now() - lastFailure.at < 3000));
  // In an open dialog it goes inside it: above the dimmed backdrop, and not made inert by it.
  const host = $$("dialog[open]").pop() || document.body;
  if (t.parentElement !== host) host.appendChild(t);
  t.classList.toggle("err", error);
  $("#toastClose").hidden = !error;
  text.textContent = "";  // cleared first, so the same message twice is announced twice
  requestAnimationFrame(() => { text.textContent = msg; placeToast(); });
  t.classList.add("show");
  placeToast();
  toastLeft = Math.max(error ? 10000 : 4000, 60 * msg.length, opts.ms || 0);
  runToast();
}
// With My list's Undo message showing too, this one goes just above it, so neither covers the other.
function placeToast() {
  const t = $("#toast"), s = $("#snack");
  const both = t.parentElement === document.body && s.classList.contains("show") && $("#mylist").classList.contains("on");
  t.style.bottom = both ? `${Math.round(innerHeight - s.getBoundingClientRect().top + 8)}px` : "";
}
// The phone tab bar and the print bar (while picked tasks keep it at the bottom), measured for the CSS:
// messages at the bottom sit above both, at any text size or screen width (the CSS uses --barH only
// while My list shows with the bar stuck).
function placeBars() {
  const bar = $(".printbar"), tabs = $(".bottombar");
  document.body.style.setProperty("--barH", `${bar.classList.contains("sticky") ? bar.offsetHeight : 0}px`);
  if (tabs?.offsetHeight) document.body.style.setProperty("--tabH", `${tabs.offsetHeight}px`);
  else document.body.style.removeProperty("--tabH");
  placeToast();
}
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => placeBars());
  for (const el of [$(".printbar"), $(".bottombar")]) if (el) ro.observe(el, { box: "border-box" });
}
function runToast() {
  clearTimeout(toastTimer);
  toastStarted = Date.now();
  toastTimer = setTimeout(hideToast, toastLeft);
}
function hideToast() {
  clearTimeout(toastTimer);
  const t = $("#toast");
  t.classList.remove("show");
  if (t.contains(document.activeElement)) document.activeElement.blur();
}
$("#toastClose").onclick = hideToast;
for (const ev of ["mouseenter", "focusin"]) $("#toast").addEventListener(ev, () => {  // reading it: wait
  if (!$("#toast").classList.contains("show")) return;
  clearTimeout(toastTimer);
  toastLeft = Math.max(1500, toastLeft - (Date.now() - toastStarted));
});
for (const ev of ["mouseleave", "focusout"]) $("#toast").addEventListener(ev, () => {
  if ($("#toast").classList.contains("show") && !$("#toast").matches(":hover") && !$("#toast").contains(document.activeElement)) runToast();
});
// A dialog that closes takes a toast shown inside it along: put it back on the page first.
document.addEventListener("close", (e) => {
  const t = $("#toast");
  if (e.target.tagName === "DIALOG" && e.target.contains(t)) document.body.appendChild(t);
}, true);

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  // The phone's status bar follows the theme picked here, not only the phone's own setting.
  $$('meta[name="theme-color"]').forEach((m) => {
    m.content = t === "dark" || (!t && m.media.includes("dark")) ? "#141414" : "#fafaf7";
  });
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
  forgetDraft();  // an unsent Brain dump stays on this device only while you're signed in
  forgetMyKeysHere();  // and so do your search words and List/Schedule pick
  startOver("Signed out.");
}
// What this device keeps per person that says something about them: an unsent Brain dump, the search
// words, List or Schedule. Signing out takes the person's own; anyone signing in here takes everyone
// else's (a session that ended without Sign out left them behind).
const PERSON_KEYS = /^nb\.(dumpDraft|listFilter|listView):/;
function forgetMyKeysHere() {
  try { for (const k of [filterKey(), myListViewKey()]) if (k) localStorage.removeItem(k); } catch {}
}
function forgetOthersHere(name) {
  try {
    for (const k of Object.keys(localStorage)) if (PERSON_KEYS.test(k) && k.slice(k.indexOf(":") + 1) !== name) localStorage.removeItem(k);
  } catch {}
}
// Belt and braces for the reload above: wipe what's on screen right away.
function clearPerson() {
  me = null; boot.started = false;
  resetDry();
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
  forgetListExtras();
  todayCal = null; viewPick = "";
  conn = null; printOpts = null; update = null; editing = null; suRows = []; suPicked = new Set(); suOpen = null;
  $("#lookOut").innerHTML = ""; $("#lookNotes").checked = false; lookText = ""; lookPrivacy();  // the AI helpers
  routineForget(); timerForget(); remindForget(); quickRoute = null; shareForget();  // FE: the card, timer, reminders, a share
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
  statusNow = null;  // /auth/me is newer than the last /status: Preview only follows it until /status answers
  if (me.must_change) { showOnly("mustChange"); $("#mcCur").focus(); return; }
  showOnly("appShell");
  $("#who").textContent = me.username;
  applyLook();
  if (!boot.started) {  // just signed in: open where they asked to
    boot.started = true;
    forgetOthersHere(me.username);  // a shared phone: nobody else's draft or search words stay behind
    resetDry();  // nor their Preview only box: loadPrefs below shows this person's own state
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
  loadRoutine();
  $("#noticeText").textContent = me.data_notice;
  $("#noticeWhen").textContent = me.consented ? `You accepted this on ${when(me.consented_at)}.` : "";
  if (!me.consented || me.terms_ok === false) {  // new, or the notice or Terms changed since they agreed
    $("#noticeBody").textContent = me.data_notice;
    $("#noticeChanged").hidden = !me.notice_changed;
    // What's new in the Privacy Policy (TERMS_VERSION 3 and 4), when that's what changed.
    $("#noticeChanged").textContent = "This changed since you last agreed. " + (me.terms_ok === false
      ? "The Privacy Policy now says just what the AI helpers (Break it down and Look back on my week) send to Anthropic, and that they only do it when you tap them. "
        + "It also says that with Journal mode off, a photo with handwriting on it is still kept as that day's journal page. " : "")
      + "Have a quick look before you carry on.";
    $("#noticeMsg").textContent = "";
    if (!$("#noticeDialog").open) $("#noticeDialog").showModal();
  }
  // A new account: the setup questions, once the notice is agreed (boot runs again after "I agree").
  else if (me.setup_quiz && !quizAsked && !$("#quizDialog").open) { quizAsked = true; openQuiz(); }
  quickRun();  // an app shortcut or a share that opened Next Box (FE): once nothing else is asking first
}

$("#noticeOk").onclick = async () => {
  try {
    await api("/auth/consent", { method: "POST" });
    me.consented = true; me.terms_ok = true;  // agreed: closing it now is for good (boot reads them again)
    $("#noticeDialog").close(); boot();
  }
  catch (e) { $("#noticeMsg").textContent = errText(e); }
};
$("#noticeNo").onclick = () => signOut($("#noticeMsg"));
// Agree, or sign out: nothing else closes it (W3-01). closedby="none" in index.html stops Esc; Esc is also
// stopped as a key, before it can become a close request, for browsers without closedby (Chrome refuses
// only one close request in a row without a tap in between). Anything that still closes it while the
// notice waits for an answer opens it again. closedby="none" also turns off the dialog's close watcher, so
// the phone's back gesture isn't caught here: it goes back as on any page, and the notice is there next time.
$("#noticeDialog").addEventListener("keydown", (e) => { if (e.key === "Escape") e.preventDefault(); });
$("#noticeDialog").addEventListener("cancel", (e) => e.preventDefault());
$("#noticeDialog").addEventListener("close", () => {
  if (me && (!me.consented || me.terms_ok === false) && !$("#appShell").hidden) $("#noticeDialog").showModal();
});

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
  try { printOpts = await api("/printing-options"); }
  catch (e) { $("#printersLoaded").hidden = false; $("#printersLoaded").textContent = errText(e); return; }
  const ps = printOpts.printers, none = !ps.length;
  // No printer: "No printer added." and the next step for this person. Your usual printer and the color
  // rules need a printer, so they're hidden; every ticket still works as a preview and on paper.
  for (const id of ["#defPrinterRow", "#ruleHead", "#colorRules"]) $(id).hidden = none;
  $("#noPrinter").hidden = !none;
  // Outside Home, the next step is PRINT_AWAY (shown here only, not again below).
  $("#noPrinterNext").textContent = !printsHere() ? PRINT_AWAY : noPrinterNext();
  $("#noPrinterGo").hidden = !none || !printsHere() || !canAddPrinter();
  $("#printersLoaded").hidden = none && !printsHere();
  $("#printersLoaded").innerHTML = none ? "Until then, every ticket can be previewed and printed on paper."
    : "Loaded right now: " + ps.map((p) => `${sw(p.stock_color)}${esc(p.name)} (${esc(p.stock_color)} labels${p.ink === "black_red" ? ", red ink" : ""})`).join(", ");
  if (none) return;
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
// No printer added: straight to Admin → Printers, with the add-a-printer form open.
$("#noPrinterGo").onclick = () => { $("#npForm").open = true; goto("admin", "#npForm"); };
$("#defPrinter").onchange = async () => {
  try {
    me.prefs = await api("/settings", { method: "PATCH", json: { default_printer: $("#defPrinter").value } });
    toast("Saved"); refreshStatus(); loadLabels();  // the header names the printer your tickets use
  } catch (e) { toast(errText(e)); }
};

let printerStatus = {};  // printer id → answered (true/false), from the last check; null: couldn't check
const printerAway = () => me?.role !== "superadmin" && me?.household_id !== "home";  // staff and Home see the card as always
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
      <button class="plain small" data-p="remove">Remove</button></div></div>`).join("") || (printerAway() ? "No printers yet." : "No printers yet. Add one below.");
  // A household admin outside Home: printers at their own home can't be reached from the Next Box computer.
  $("#prAway").textContent = PRINT_AWAY;
  $("#prAway").hidden = !printerAway() || ps.length > 0;
  $("#prSameNet").hidden = !printerAway();
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
  $("#npForm").open = true; $("#npForm").scrollIntoView({ behavior: smooth(), block: "nearest" }); $("#npName").focus();
}
function resetPrinterForm() {
  delete $("#npGo").dataset.pid; $("#npGo").textContent = "Add printer";
  $("#npTitle").textContent = "Add a printer"; $("#npCancel").hidden = true; $("#npMsg").textContent = "";
  $("#npName").value = $("#npAddress").value = $("#npHeight").value = "";
  $("#npColor").value = "white"; $("#npInk").checked = false; $("#npModel").value = "QL-1110NWB"; $("#npLabel").value = "62";
  if (printOpts?.drivers && $("#npDriver").options.length) driverHints();
}
$("#npCancel").onclick = () => { resetPrinterForm(); $("#npForm").open = false; };
$("#npGo").onclick = () => hold($("#npGo"), savePrinter);  // one tap, one printer
async function savePrinter() {
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
    loadPrinters(); loadLabels(); refreshStatus();
  } catch (e) { $("#npMsg").textContent = errText(e); }
}
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
      refreshStatus();  // the header, and whether this household still prints here
    } else {
      const real = b.dataset.p === "print";
      if (real && !confirm(`Send a real test print to ${p.name}?`)) return;
      const r = await api(`/admin/printers/${encodeURIComponent(pid)}/test?dry_run=${!real}`, { method: "POST" });
      const sent = r.status === "printed";
      const name = esc(r.printer.name);
      $("#prOut").innerHTML = `<p>${sent ? `Printed on ${name}:` : real ? `Printing is paused or set to preview, so nothing went to ${name}. Here's the preview:` : `Preview for ${name}:`}</p>
        <img src="${png(r.id)}" alt="Test label for ${esc(r.printer.name)}" style="max-width:100%;border:1px solid var(--line);border-radius:6px">`;
      $("#prOut").scrollIntoView({ behavior: smooth(), block: "nearest" });
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
    $("#uChart").innerHTML = s.map((d) => `<div style="height:${Math.round(total(d) / Math.max(1, max) * 100)}%" title="${week ? "Week of " : ""}${day(d.date)}: ${plural(d.tickets, "ticket")}, ${plural(d.scans, "read-back")}, ${d.people} ${d.people === 1 ? "person" : "people"}"></div>`).join("");
    $("#uChart").setAttribute("aria-label", `${week ? "Weekly" : "Daily"} tickets and read-backs, ${day(s[0].date)} to ${day(s[s.length - 1].date)}: ` +
      (max ? `most was ${max} ${week ? "in the week of" : "on"} ${day(peak.date)}` : "none yet"));
    $("#uChartCap").innerHTML = `<span>${day(s[0].date)}</span><span>${week ? "per week" : "per day"} · most ${max}</span><span>${day(s[s.length - 1].date)}</span>`;
    $("#uPeople").innerHTML = u.people.map((p) => `<div class="card"><div class="top" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <b>${esc(p.username)}</b><span class="badge ${p.role !== "user" ? "hot" : ""}">${esc(p.role)}</span>${p.beta ? `<span class="badge">beta</span>` : ""}
        <span class="muted" style="margin-left:auto">active ${when(p.last_active)}</span></div>
      <div class="muted">${p.app ? esc(p.app) : "no to-do app"} · ${p.consented ? "notice accepted " + when(p.consented_at) : "<span class='err'>notice not accepted yet</span>"}</div>
      <div style="margin-top:6px">${plural(p.tickets, "ticket")} (${p.printed} printed) · ${plural(p.scans, "read-back")} · marks: ${p.marks_applied} auto, ${p.marks_confirmed} confirmed, ${p.marks_skipped} skipped</div>
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
  $("#uTickets").innerHTML = `<div class="tickets">${d.tickets.map((r) => `<div><img loading="lazy" src="${png(r.id)}" alt="Ticket #${esc(r.id)}">
      <div class="muted">${esc(r.created_at.replace("T", " "))}${r.printer ? " · " + esc(r.printer.name) : ""}${r.dry_run ? " · preview" : ""}</div></div>`).join("") || "<p class='muted'>No tickets.</p>"}</div>
    <h3>Read-backs</h3>${d.scans.map((r) => `<div class="muted">${esc(r.created_at.replace("T", " "))}: ${r.applied.length} applied, ${r.needs_confirmation.length} needed a tap${r.errors.length ? `, ${r.errors.length} problem${r.errors.length === 1 ? "" : "s"}` : ""}</div>`).join("") || "<p class='muted'>None.</p>"}`;
  $("#uDetail").scrollIntoView({ behavior: smooth() });
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
      providers.filter((p) => p.status !== "ready" && p.status !== "oauth").map((p) => p.status === "later"
        ? `<p><b>${esc(p.name)}</b>: not yet. <button class="linkish" data-feedback data-ask-app="${esc(p.name)}">Ask for ${esc(p.name)}</button></p>`
        : `<p><b>${esc(p.name)}</b>: ${p.status === "needs_setup" ? "not switched on yet." : "not possible."} ${esc(p.reason)}</p>`).join("");
    renderProviderFields();
  }
}

// Where Print tab words point: new tasks go to My list unless Settings says the linked app.
function setAppNames() {
  const linked = conn?.connected ? conn.name : "";
  $$(".newTasksTo").forEach((el) => el.textContent = linked && me?.prefs?.new_tasks_to === "linked" ? linked : "my list");
  $$(".todaySources").forEach((el) => el.textContent = linked ? `my list and ${linked}` : "my list");
  $$(".maxRows").forEach((el) => el.textContent = me?.prefs?.max_rows || 5);
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
const loaders = { mylist: () => { loadList(); loadToday(); loadGuide(); loadRoutine(); }, printed: () => loadHistory(), profile: () => loadProfile(),
                  print: () => loadPrintTab(),
                  settings: () => { loadLabels(); loadTags(); loadCalendars(); }, admin: () => { if (me.role !== "superadmin") loadAdmin(); loadPrinters(); loadInvites(); },
                  debug: loadDebug, board: () => loadBoard(), usage: () => loadUsage(), journal: () => loadJournal() };
function markTab(tab) {  // which screen shows, without loading anything
  $$("#topnav button").forEach((x) => { x.classList.toggle("on", x.dataset.tab === tab); x.toggleAttribute("aria-current", x.dataset.tab === tab); if (x.dataset.tab === tab) x.setAttribute("aria-current", "page"); });
  $$(".bottombar [data-go]").forEach((x) => { x.classList.toggle("on", x.dataset.go === tab); setCurrent(x, x.dataset.go === tab); });
  $("#moreBtn").classList.toggle("on", !["mylist", "print", "scan", "journal"].includes(tab));
  setCurrent($("#moreBtn"), !["mylist", "print", "scan", "journal"].includes(tab));
  // Where you are, also in the browser tab and the app switcher: "Journal · Next Box".
  const name = $(`#topnav button[data-tab="${tab}"]`)?.firstChild?.textContent.trim();
  document.title = me && name ? `${name} · Next Box` : "Next Box";
  $$("section").forEach((s) => s.classList.toggle("on", s.id === tab));
  $("#dryRow").hidden = !["mylist", "print", "printed"].includes(tab);  // only where things get printed
}
const setCurrent = (el, on) => { if (on) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); };
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
    if (me && typeof s.prints_here === "boolean") me.prints_here = s.prints_here;  // a printer was added or removed
    $("#status").textContent = !s.printer ? (printsHere() ? "No printer added · print on paper" : "Previews · print from your device")
      : `${s.printer} ${s.printer_reachable ? "online" : "offline"}${s.dry_run_forced ? " · preview only" : ""}`;
  } catch (e) { $("#status").textContent = `server: ${e.message}`; }
  applyDry();
}
// Who can add a printer: admins and staff, who also see Admin → Printers (the server decides, as always).
const canAddPrinter = () => !!me && RANK[me.role] >= RANK.admin;
// Home (or a household that prints here) with no printer: "No printer added", then the next step for them.
const NO_PRINTER = "No printer added.";
const noPrinterNext = () => (canAddPrinter() ? "Add one in Admin → Printers." : "Ask your Next Box admin to add one.");
// Whether a real print can happen: /auth/me carries the same flags as /status (printing paused, previews
// only, how many printers), so the answer is there the moment someone signs in or the page loads, without
// waiting for /status (up to 2 s on whether the printer is online). Once /status answers, it's the newer one.
const printNow = () => statusNow || me || {};
// Printing is the default: "Preview only" starts unticked. It's ticked and locked, with the reason, whenever
// a real print can't happen anyway, so nobody is asked "Send this to the printer?" for something that will
// only be a preview. When that reason goes away (a printer added, the setting turned off) it's unticked
// again; a tick the person made themselves stays. A real print always asks "Send this to the printer?" first.
function applyDry() {
  const st = printNow();
  const why = st.printing_paused ? "Printing is paused by an admin."
    : !printsHere() ? PRINT_AWAY
    : me?.prefs?.always_dry_run ? "Always preview only is on in Settings."
    : st.dry_run_forced ? (me?.role !== "superadmin" ? "This Next Box makes previews only for now."
      // The owner can change it: say how.
      : st.printers ? "Previews only: NEXTBOX_DRY_RUN=1 is in .env. Delete that line and run: systemctl --user restart nextbox"
      : "Previews only: NEXTBOX_DRY_RUN=1 is in .env. Add a printer in Admin → Printers (or set PRINTER_IP), then delete that line and run: systemctl --user restart nextbox")
    : st.printers === 0 ? "No printer added: print previews on paper." + (canAddPrinter() ? " Add one in Admin → Printers." : "")
    : "";
  const box = $("#dry"), was = box.checked;
  if (why) { box.checked = true; box.dataset.forced = "1"; }
  else if (box.dataset.forced) { box.checked = false; delete box.dataset.forced; }
  box.disabled = !!why;
  $("#dryWhy").textContent = why;
  if (box.checked !== was) box.dispatchEvent(new Event("change"));  // "Preview today's label" or "Print today's label"
}
// A new person on this device starts from the default (printing), never from the last person's box.
function resetDry() {
  statusNow = null;
  const box = $("#dry");
  box.checked = box.disabled = false; delete box.dataset.forced;
  $("#dryWhy").textContent = "";
}

// ---------------------------------------------------------------- print
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const smooth = () => (reducedMotion() ? "auto" : "smooth");  // every scroll: no long glide when motion is reduced
// A result is shown where it can be seen: scrolled to, and its heading focused (and announced).
function revealResult(box) {
  box.hidden = false;
  box.scrollIntoView({ behavior: smooth(), block: "start" });
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
    ${paperButton(t.id)}<img src="${png(t.id)}" alt="${esc(ticketAlt(t))}">`;
}
// Says which ticket this is and what's on it, so tickets can be told apart without seeing them.
function ticketAlt(t) {
  const names = (t.manifest || []).map((m) => m.content).filter(Boolean);
  const head = `${t.status === "printed" ? "Printed" : "Preview of"} ticket #${t.id}`;
  if (!names.length) return t.kind === "text" && t.title ? `${head}: ${t.title}` : head;
  const shown = names.slice(0, 5).join("; ");
  return `${head}, ${names.length} task${names.length === 1 ? "" : "s"}: ${shown}${names.length > 5 ? `; and ${names.length - 5} more` : ""}`;
}
function showResultError(box, msg) {
  box.innerHTML = `<h2 tabindex="-1">Couldn't print</h2><p class="err">${esc(msg)}</p>`;
  revealResult(box);
}
// While a print is on its way the button says so (and can't be pressed twice); returns "put it back".
// aria-disabled, not disabled: a disabled button drops keyboard focus to the top of the page.
function busy(b, dry) {
  const html = b.innerHTML;
  b.setAttribute("aria-disabled", "true"); b.setAttribute("aria-busy", "true");
  b.textContent = dry ? "Making preview…" : "Printing…";
  return () => { b.removeAttribute("aria-disabled"); b.removeAttribute("aria-busy"); b.innerHTML = html; };
}
// Runs fn while btn can't be pressed again (a double tap must not save twice), keeping focus on btn.
async function hold(btn, fn) {
  if (!btn || btn.getAttribute("aria-disabled") === "true") return;
  btn.setAttribute("aria-disabled", "true");
  try { return await fn(); } finally { btn.removeAttribute("aria-disabled"); }
}
// A button marked aria-disabled ignores clicks, taps, Enter and Space (and won't submit its form),
// like a disabled one, but keeps focus.
document.addEventListener("click", (e) => {
  if (e.target.closest?.('button[aria-disabled="true"], a[aria-disabled="true"]')) { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);
// Buttons that are still disabled while they work (Add to my list, Read back, Connect…): when one had
// focus, it gets it back once it works again, unless you've moved on.
const parked = new WeakSet();
let lastLeft = { el: null, at: 0 };
document.addEventListener("focusout", (e) => { lastLeft = { el: e.target, at: Date.now() }; });
new MutationObserver((changes) => {
  for (const { target: b } of changes) {
    if (b.disabled && (document.activeElement === b || (lastLeft.el === b && Date.now() - lastLeft.at < 1000))) parked.add(b);
    else if (!b.disabled && parked.has(b)) {
      parked.delete(b);
      if (b.isConnected && (!document.activeElement || document.activeElement === document.body)) b.focus({ preventScroll: true });
    }
  }
}).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["disabled"] });

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
    if (act === "today") loadRoutine();
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
           "read before": `Already read from this ticket: ${what}`,
           "deleted since printed": `${i.content} was deleted after this ticket printed, so nothing changed` }[i.reason] || `Row ${i.row}: ${i.reason}`;
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
      : "";  // the message above says there's no ticket yet
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
    : p.reason === "deleted since printed" ? `<p class="muted">${esc(ignoredLine(p))}</p>`
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
      // Carry on where you were: the next mark still waiting, else the card's heading.
      (fresh.querySelector("[data-d=confirm]") || fresh.querySelector("h2")).focus({ preventScroll: true });
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
  card.scrollIntoView({ behavior: smooth(), block: "start" });
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
// No AI key (Snap, Break it down, Look back): the owner (a superadmin) is told how to add one, everyone
// else who to ask, as the server says (coach.NO_KEY_OWNER / coach.NO_KEY).
const NO_KEY_STEPS = "Add ANTHROPIC_API_KEY to .env (console.anthropic.com → API keys), then run: systemctl --user restart nextbox";
const noKeyText = () => (me?.role === "superadmin" ? `This needs an AI key. ${NO_KEY_STEPS}` : "Ask your Next Box owner to add an AI key.");
// Snap reads photos with Claude. Without an AI key, say so here instead of offering a photo that can't be read.
function applySnapReady() {
  const ready = me?.scan_ready !== false;
  $("#snapCam").disabled = $("#snapLib").disabled = !ready;
  $("#snapNoKey").hidden = ready;
  $("#snapNoKey").textContent = `Snap needs an AI key to read photos. ${me?.role === "superadmin" ? NO_KEY_STEPS : "Ask your Next Box owner to add one."}`;
}

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
    loadWaiting(true); loadRecent(); loadRoutine();
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
    window.scrollTo({ top: 0, behavior: smooth() });
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
    if (el === document.activeElement && ["text", "number", "time"].includes(el.type)) return;  // still typing: it saves as they leave it
    const v = prefs[el.dataset.pref];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  confText(prefs.confidence);
  remindShowPrefs(prefs);  // "Show a notification too" says what this device allows
  applyAi(prefs.ai_helpers !== false);
  applySnapReady();
  applyDry();  // no force-tick here: printing is the default, and applyDry ticks it only with a reason
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
  if (!msg) { box?.remove(); delete el.dataset.bad; delete el.dataset.retry; el.removeAttribute("aria-invalid"); return; }
  if (!label) return toast(msg, 4000);
  if (!box) { box = document.createElement("span"); box.className = "err"; box.setAttribute("role", "alert"); label.append(box); }
  box.textContent = msg; el.dataset.bad = "1"; el.setAttribute("aria-invalid", "true");
}

// One save at a time, in the order they were made: a number box saves as it loses focus, and the switch
// tapped next waits for it, so each answer has both. Only the newest answer redraws the settings (an older
// one would put back a switch tapped since), and never the box being typed in, so the screen never goes
// back to an older one.
let prefQueue = Promise.resolve(), prefPending = 0;
$$("[data-pref]").forEach((el) => el.onchange = () => {
  const k = el.dataset.pref, typed = el.type === "text" || el.type === "number" || el.type === "time";
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number" || "int" in el.dataset) v = parseInt(v, 10);  // data-int: a list of numbers
  if (el.type === "range") v = parseFloat(v);
  delete el.dataset.retry;
  prefPending++;
  return (prefQueue = prefQueue.then(() => savePref(el, k, v, typed)));
});
// A typed setting that couldn't reach Next Box tries again as they leave its box, even with the same value.
$$("[data-pref]").forEach((el) => el.addEventListener("blur", () => { if (el.dataset.retry) el.onchange(); }));
async function savePref(el, k, v, typed) {
  const newest = () => prefPending === 1;  // no later save is waiting
  try {
    const prefs = await api("/settings", { method: "PATCH", json: { [k]: v } });
    if (me) { me.prefs = prefs; applyLook(); }
    if (typed) prefErr(el, "");
    if (newest()) loadPrefs(prefs);
    if (k === "always_dry_run") refreshStatus();  // the header and the Preview box follow it
    if (k === "time_24h" || k === "gentle_words") { renderList(); loadToday(); }  // times and words on My list follow it
    if (k === "routine_mode") forgetListView();  // My list opens on its new default view (Schedule when on)
    if (["routine_mode", "routine_morning", "routine_evening"].includes(k)) renderList();
    if (["routine_card", "journal_mode"].includes(k)) routineRender();
    if (k === "remind_before") remindRender();
    toast("Saved");
  } catch (e) {
    if (typed) {
      prefErr(el, e.status ? errText(e) : `${errText(e)} It saves when you leave this box again.`);
      if (!e.status) el.dataset.retry = "1";
    } else toast(errText(e), 4000);
    if (newest() && me) loadPrefs(me.prefs);  // what is saved, apart from a box that says why it isn't
  } finally { prefPending--; }
}
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
  const at = $$("#sessions [data-endsession]").indexOf(b);
  await loadSessions();  // the list is drawn again: focus goes to the next device's button, or the heading
  const left = $$("#sessions [data-endsession]");
  focusFirst(left[Math.min(at, left.length - 1)], $("#sessions").closest(".card")?.querySelector("h2"));
});
$("#signoutOthers").onclick = () => hold($("#signoutOthers"), async () => {
  try {
    const r = await api("/auth/signout-others", { method: "POST" });
    toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? "" : "s"}`);
  } catch (e) { toast(`Couldn't sign out your other devices: ${errText(e)}`, { error: true }); }
  loadSessions();
});

// ---------------------------------------------------------------- feedback (anonymous)
let fbMode = "guided";
const currentPage = () => (me ? document.querySelector("#topnav button.on")?.dataset.tab : "login") || "";

// One chip on in a group (el null: none). aria-pressed says which, not only the blue fill.
function pick(group, el) { $$(`${group} button`).forEach((b) => { b.classList.toggle("on", b === el); b.setAttribute("aria-pressed", String(b === el)); }); }
$$("#fbType button").forEach((b) => b.onclick = () => pick("#fbType", b));
$$("#fbRating button").forEach((b) => b.onclick = () => pick("#fbRating", b.classList.contains("on") ? null : b));
$$(".seg button").forEach((b) => b.onclick = () => {
  fbMode = b.dataset.mode; pick(".seg", b);
  $("#fbGuided").hidden = fbMode !== "guided"; $("#fbOpen").hidden = fbMode !== "open";
});

function openFeedback() {
  // After one was sent, a fresh start. Closed before Send, what was typed is still there (W3-02).
  if (!$("#fbDone").hidden) { $("#fbForm").querySelectorAll("textarea").forEach((t) => t.value = ""); pick("#fbRating", null); }
  $("#fbErr").textContent = ""; $("#fbDone").hidden = true; $("#fbSend").hidden = false;
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
  el.innerHTML = `<p tabindex="-1" data-temphead>Give <b>${esc(u)}</b> this one-time password. It won't be shown again, and they'll pick their own when they sign in.</p>
    <div class="secret">${esc(pw)}</div>
    <div class="row"><button class="plain small" data-copy>Copy</button></div>`;
  el.querySelector("[data-copy]").onclick = () => copyText(pw);
  el.scrollIntoView({ behavior: smooth(), block: "center" });
  el.querySelector("[data-temphead]").focus({ preventScroll: true });  // a screen reader says it's there (W3-06); Tab goes on to Copy
}
function showTemp(u, pw) { renderTemp($("#nuOut"), u, pw); }

$("#nuGo").onclick = () => hold($("#nuGo"), async () => {  // a double tap mustn't make a second one-time password
  try {
    const r = await api("/admin/users", { method: "POST", json: { username: $("#nuName").value, role: $("#nuRole").value, beta: $("#nuBeta").checked } });
    $("#nuName").value = "";
    showTemp(r.username, r.temp_password);
    loadAdmin();
  } catch (e) { toast(errText(e), { error: true }); }
});

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
  $("#activityCard").scrollIntoView({ behavior: smooth() });
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
let listFilter = { q: "", tag: "" }, filterFor = "";  // My list's search and tag, remembered per person on this device
let shownOpen = [];         // the open tasks the list shows now (what "Prints the N shown" prints)
const openSteps = new Set();  // rows whose tiny steps are shown
let nowAsk = null;            // Just one thing: the task whose last step was just ticked ("Finish the task?")
const tagColor = (name) => (listData.tags.find((t) => t.name === name) || {}).color || "white";
const tagChip = (name) => { const c = tagColor(name); return `<span class="tag tc-${esc(c)}${c === "red" ? " solid" : ""}">${esc(name)}</span>`; };
// A tag on a row is a button: it shows only the tasks with that tag.
const tagButton = (name) => { const c = tagColor(name); return `<button type="button" class="tag tc-${esc(c)}${c === "red" ? " solid" : ""}" data-ftag="${esc(name)}" title="Show only #${esc(name)}" aria-label="${esc(name)}: show only this tag">${esc(name)}</button>`; };
const today = () => new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD, local
const dayAfter = (iso, n = 1) => { const d = new Date(iso + "T12:00"); d.setDate(d.getDate() + n); return d.toLocaleDateString("en-CA"); };
// "15:30" as Settings → 24-hour times says: 15:30, or 3:30p like the tickets.
const fmtClock = (hhmm) => { const h = +hhmm.slice(0, 2); return me?.prefs?.time_24h ? hhmm : `${h % 12 || 12}:${hhmm.slice(3, 5)}${h < 12 ? "a" : "p"}`; };
const MAX_PICK = 30;  // stickers at once (the server says the same)
const shortName = (t) => (t.content.length > 60 ? t.content.slice(0, 57) + "…" : t.content);

function whenText(t) {
  if (!t.due) return "";
  const d = t.due.date.slice(0, 10), now = today();
  const time = t.due.date.length > 10 ? " " + fmtClock(t.due.date.slice(11, 16)) : "";
  if (d < now) {
    const days = Math.round((new Date(now) - new Date(d)) / 86400000);
    return `${me?.prefs?.gentle_words === false ? "overdue" : "waiting"} ${days}d`;
  }
  if (d === now) return "today" + time;
  if (d === dayAfter(now)) return "tomorrow" + time;  // as stickers and the focus card say it
  return new Date(d + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + time;
}

function doneText(ts) {
  const d = new Date(ts * 1000), day = d.toLocaleDateString("en-CA");
  if (day === today()) return "done today";
  if (day === dayAfter(today(), -1)) return "done yesterday";
  return "done " + d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function taskRow(t) {
  const steps = t.steps || [];
  const doneSteps = steps.filter((s) => s.done).length;
  const local = String(t.id).startsWith("nb:");
  const sid = "st-" + String(t.id).replace(/[^\w-]/g, "_");
  const showSteps = local && !t.done && steps.length > 0 && openSteps.has(t.id);
  const slid = !t.done && (listData.slides || {})[t.id];  // a slug, as the Journal counts them
  const bits = (t.done ? [esc(t.done_at ? doneText(t.done_at) : "done")] :
    [esc(whenText(t)), t.repeat ? esc("↻ " + t.repeat) : "", t.minutes ? esc(`~${t.minutes}m`) : "",
     !steps.length ? "" : local ? `<button type="button" class="linkish stepsbtn" data-steps aria-expanded="${showSteps}" aria-controls="${sid}">steps ${doneSteps}/${steps.length}<span class="sr-only"> done</span></button>`
       : esc(`steps ${doneSteps}/${steps.length}`),
     slid ? `<span class="slid" title="Moved to a later day ${slid} times"><span aria-hidden="true">slid ${slid}×</span><span class="sr-only">moved to a later day ${slid} times</span></span>` : "",
     t.source && t.source !== "nextbox" ? esc("in " + (listData.linked_name || t.source)) : ""])
    .filter(Boolean).join(" · ");
  const meta = bits + (t.description ? `${bits ? " " : ""}<span aria-hidden="true" title="Has notes">📝</span><span class="sr-only">has notes</span>` : "");
  const name = esc(shortName(t));
  const full = !picked.has(t.id) && picked.size >= MAX_PICK;
  const canMove = !t.done && (!t.due || t.due.date.slice(0, 10) <= today());  // "→ Tomorrow" never moves a task sooner
  const stepList = showSteps ? `<ul class="rowsteps" id="${sid}" aria-label="Tiny steps">${steps.map((s, i) => `<li><label>
      <input type="checkbox" data-step="${i}" ${s.done ? "checked" : ""}><span>${esc(s.text)}</span></label></li>`).join("")}</ul>` : "";
  return `<div class="task${t.done ? " done-row" : ""}" data-id="${esc(t.id)}">
    ${t.done ? "" : `<label class="pickbox"><input type="checkbox" class="pick" aria-label="Pick “${name}”" ${picked.has(t.id) ? "checked" : ""}${full ? " disabled" : ""}></label>`}
    <div class="body">
      <div class="text${t.priority >= 3 ? " urgent" : ""}">${(t.labels || []).map(tagButton).join("")}${t.priority >= 4 ? "!! " : t.priority >= 3 ? "! " : ""}${esc(t.content)}</div>
      ${meta ? `<div class="meta">${meta}</div>` : ""}${stepList}
    </div>
    <div class="acts">
      ${t.done ? (local ? `<button class="plain small" data-reopen aria-label="Undo: “${name}” isn't done">Undo</button>` : "") :
        `${local ? `<button class="plain small" data-edit aria-label="Edit “${name}”">Edit</button>` : ""}${canMove
          ? `<button class="move" data-move aria-label="Move “${name}” to tomorrow" title="Move to tomorrow">→</button>` : ""}<button class="done" data-done aria-label="Mark “${name}” done">✓</button>`}
    </div></div>`;
}

// --- search and tag filter (this device remembers each person's last one) ---
const filterKey = () => (me ? `nb.listFilter:${me.username}` : "");
function loadFilter() {
  if (!me || filterFor === me.username) return;
  filterFor = me.username;
  listFilter = { q: "", tag: "" };
  try {
    const v = JSON.parse(localStorage.getItem(filterKey()) || "null");
    if (v && typeof v === "object") listFilter = { q: String(v.q || "").slice(0, 200), tag: String(v.tag || "").slice(0, 40) };
  } catch {}
  $("#listSearch").value = listFilter.q;
}
function saveFilter() {
  try {
    const k = filterKey();
    if (k) listFilter.q.trim() || listFilter.tag ? localStorage.setItem(k, JSON.stringify(listFilter)) : localStorage.removeItem(k);
  } catch {}
}
const filtering = () => !!(listFilter.q.trim() || listFilter.tag);
const fold = (s) => String(s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();  // café → cafe
function matches(t) {  // every word somewhere in the task, its #tags, notes or steps; accents and case don't matter
  if (listFilter.tag && !(t.labels || []).includes(listFilter.tag)) return false;
  const words = fold(listFilter.q).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = fold([t.content, t.description, ...(t.labels || []).map((l) => "#" + l), ...(t.steps || []).map((s) => s.text)].join(" \n "));
  return words.every((w) => hay.includes(w));
}
function setFilter(change, focusTag = null) {
  listFilter = { ...listFilter, ...change };
  saveFilter();
  renderList();
  if (focusTag !== null) $$("#listTags [data-ftag]").find((b) => b.dataset.ftag === focusTag)?.focus();
}
function renderTagFilter() {  // the tags on open tasks, most used first; "All" clears
  const counts = new Map();
  for (const t of [...listData.tasks.filter((x) => !x.done), ...(listData.linked || [])]) {
    for (const l of new Set(t.labels || [])) counts.set(l, (counts.get(l) || 0) + 1);
  }
  if (listFilter.tag && !counts.has(listFilter.tag)) counts.set(listFilter.tag, 0);
  const names = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b));
  $("#listTags").hidden = !names.length;
  $("#listTags").innerHTML = !names.length ? "" : `<button type="button" data-ftag="" aria-pressed="${!listFilter.tag}">All</button>` +
    names.map((n) => `<button type="button" data-ftag="${esc(n)}" class="tc-${esc(tagColor(n))}" aria-pressed="${listFilter.tag === n}">
      <span class="tdot" aria-hidden="true"></span>${esc(n)} <span class="n">${counts.get(n)}</span></button>`).join("");
}
$("#listTags").addEventListener("click", (e) => {
  const b = e.target.closest("[data-ftag]");
  if (b) setFilter({ tag: b.dataset.ftag === listFilter.tag ? "" : b.dataset.ftag }, b.dataset.ftag === listFilter.tag ? "" : b.dataset.ftag);
});
let searchTimer = null;
$("#listSearch").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => setFilter({ q: $("#listSearch").value.slice(0, 200) }), 150);
});
$("#listFilterClear").onclick = () => { $("#listSearch").value = ""; setFilter({ q: "", tag: "" }); $("#listSearch").focus(); };
// List and Schedule both: the chips, "Showing 2 of 9 matching …" and what "Prints the N shown" prints
// (the open tasks on screen now).
function applyFilterUi(openShown, total) {
  shownOpen = openShown;
  renderTagFilter();
  const words = listFilter.q.trim();
  $("#listFilterSay").textContent = !filtering() ? "" : `Showing ${shownOpen.length} of ${total}` +
    (listFilter.tag ? ` with #${listFilter.tag}` : "") + (words ? ` matching “${words}”` : "");
  $("#listFilterClear").hidden = !filtering();
}
function noMatchHtml(where) {  // where: "on your list", "today"
  const words = listFilter.q.trim();
  return `<p class="muted">Nothing ${where} matches ${[words && `“${esc(words)}”`, listFilter.tag && `#${esc(listFilter.tag)}`].filter(Boolean).join(" with ")}.
    <button class="linkish" data-clear-filter>Show everything</button></p>`;
}
function forgetListExtras() {  // signing out: nothing of this person's list stays on screen
  listFilter = { q: "", tag: "" }; filterFor = ""; shownOpen = []; openSteps.clear(); nowAsk = null;
  clearTimeout(previewTimer); previewN++;
  $("#dumpPreview").hidden = true; $("#dumpLines").innerHTML = ""; $("#dumpDropped").textContent = ""; $("#dumpPreviewSay").textContent = "";
  $("#listTags").innerHTML = ""; $("#listTags").hidden = true; $("#listFilterSay").textContent = ""; $("#bulkActs").hidden = true;
}

// Where keyboard focus was in the list, so it comes back to the same place after the list is redrawn
// (or to the next task's ✓ when that task is gone, or else the My list heading).
let listFocus = null;
function focusSpot(el) {
  const row = el?.closest?.("#listBody .task");
  if (!row) return null;
  const kind = el.dataset?.step != null ? `[data-step="${el.dataset.step}"]`
    : ["[data-done]", "[data-edit]", "[data-reopen]", "[data-move]", "[data-steps]", ".pick"].find((s) => el.matches(s)) || "[data-done]";
  const after = [];
  for (let r = row.nextElementSibling; r; r = r.nextElementSibling) if (r.dataset.id) after.push(r.dataset.id);
  return { id: row.dataset.id, kind, after };
}
function restoreFocus(spot) {
  if (!spot) return;
  const rowOf = (id) => $$("#listBody .task").find((r) => r.dataset.id === id);
  const same = rowOf(spot.id)?.querySelector(spot.kind);
  if (same) return same.focus({ preventScroll: true });
  for (const id of spot.after) {
    const el = rowOf(id)?.querySelector(spot.kind === ".pick" ? ".pick" : "[data-done], [data-reopen]");
    if (el) return el.focus({ preventScroll: true });
  }
  $("#listTitle").focus({ preventScroll: true });
}
// Keyboard focus somewhere sensible when what had it went away (a sheet closed over a screen it didn't
// open from): the routine card or Just one thing on My list, else the screen's first heading.
function focusMain() {
  const el = [$("#routineCard:not([hidden]) h2"), $("#nowText"), ...$$("section.on h2")].find((x) => x?.offsetParent);
  if (!el) return;
  if (!el.hasAttribute("tabindex")) el.tabIndex = -1;
  el.focus({ preventScroll: true });
}
// After something you were on is removed: keyboard focus goes to the first of these still on screen
// (a heading gets tabindex=-1), never back to the top of the page. The page only moves if that is off
// screen or under a bar (the scroll padding), and then just enough to show it (W3-07).
function focusFirst(...els) {
  const el = els.find((x) => x?.isConnected && x.offsetParent);
  if (!el) return focusMain();
  if (!el.matches("button, a[href], input, select, textarea, [tabindex]")) el.tabIndex = -1;
  el.focus({ preventScroll: true });
  const r = el.getBoundingClientRect(), pad = getComputedStyle(document.documentElement);
  if (r.top < (parseFloat(pad.scrollPaddingTop) || 0) || r.bottom > innerHeight - (parseFloat(pad.scrollPaddingBottom) || 0)) {
    el.scrollIntoView({ block: "nearest" });
  }
}

function renderList() {
  const spot = listFocus || focusSpot(document.activeElement);
  listFocus = null;
  loadFilter();
  if (markListView() === "schedule") return renderSchedule(spot);  // List | Schedule (below)
  const now = today();
  const all = listData.tasks.filter((t) => !t.done);  // the server sends them in ticket order
  const shown = filtering() ? matches : () => true;
  const open = all.filter(shown);
  const done = listData.tasks.filter((t) => t.done && shown(t));
  const linked = (listData.linked || []).filter(shown);
  const groups = [
    ["Today", open.filter((t) => t.due && t.due.date.slice(0, 10) <= now), "today"],
    ["Coming up", open.filter((t) => t.due && t.due.date.slice(0, 10) > now)],
    ["Someday", open.filter((t) => !t.due)],
    [`From ${listData.linked_name || "linked app"} (today)`, linked],
    ["Done", showingDone ? done : [], "done"],
  ];
  const html = groups.filter(([, ts, key]) => ts.length || (key === "done" && showingDone && !filtering()))
    .map(([name, ts, key]) => `<div class="group">${esc(name)} (${ts.length})${key === "today" && ts.length > 1
        ? ` <button class="linkish" data-pickall>Pick all</button>` : ""}</div>`
      + (ts.length ? ts.map(taskRow).join("") : `<p class="muted">Nothing finished yet. It'll show here when you tick something.</p>`)).join("");
  const total = all.length + (listData.linked || []).length;
  $("#listBody").innerHTML = html || (filtering() && total ? noMatchHtml("on your list")
    : `<p class="muted">Your list is empty. Use Brain dump above, or + Add task.</p>`);
  $("#listBody").classList.toggle("muted", !html);
  applyFilterUi(groups.filter(([, , key]) => key !== "done").flatMap(([, ts]) => ts), total);
  updatePicks();
  renderNow();
  restoreFocus(spot);
}

// The print bar: how many are picked, Clear, and the 30-at-once limit shown before pressing. With a search
// or a tag on and nothing picked, it prints what's shown. Picked tasks get Done, → Tomorrow, Tag… and Delete….
function updatePicks() {
  const n = picked.size;
  const hidden = filtering() || myListView() === "schedule" ? [...picked].filter((id) => !shownOpen.some((t) => t.id === id)).length : 0;
  const m = Math.min(shownOpen.length, MAX_PICK);
  $("#pickCount").textContent = n ? (n >= MAX_PICK ? `${n} picked (the most at once)` : `${n} picked`) + (hidden ? ` · ${hidden} not shown` : "")
    : !filtering() ? "Nothing picked: stickers for today's tasks"
    : !m ? "Nothing shown to print" : shownOpen.length > MAX_PICK ? `Prints the first ${m} shown` : `Prints the ${m} shown`;
  $("#pickClear").hidden = !n;
  $("#bulkActs").hidden = !n;
  $$("#listBody .pick").forEach((b) => { b.disabled = !b.checked && n >= MAX_PICK; });
  $(".printbar").classList.toggle("sticky", n > 0);  // it follows you down the list only while something is picked
  placeBars();
}
// What Stickers and Tear-off strips print: the picked tasks, else the ones shown, else today's.
function printPicks() {
  if (picked.size) return { task_ids: [...picked] };
  if (filtering()) return shownOpen.length ? { task_ids: shownOpen.slice(0, MAX_PICK).map((t) => t.id) } : null;
  return {};
}

function renderNow() {
  const hadFocus = $("#nowCard").contains(document.activeElement);
  const open = [...listData.tasks.filter((t) => !t.done), ...(listData.linked || [])];
  const dueNow = open.filter((x) => x.due && x.due.date.slice(0, 10) <= today());
  const one = listData.one_thing;
  // A focus timer that's on keeps its task here; else the list's copy of the one thing (a ticked step shows at once).
  let t = timerTask(open) || (one && !skipped.has(one.id) ? open.find((x) => x.id === one.id) || null : null);
  if (!t) t = dueNow.find((x) => !skipped.has(x.id)) || null;  // next in ticket order
  const setAside = dueNow.filter((x) => skipped.has(x.id)).length;
  // Brand new: nothing on the list now or ever (finishing your last task is a win, not a fresh start).
  const brandNew = !listData.tasks.length && !(listData.linked || []).length && !listData.has_tasks && !listData.done_today;
  $("#nowCard").dataset.id = t ? t.id : "";
  const text = $("#nowText");
  text.textContent = t ? t.content
    : brandNew ? "Your one thing will show here."
    : setAside ? `You've set aside ${setAside} for now.`
    : listData.done_today ? "Nothing else is due today. Pick anything, or rest."
    : "Nothing due today.";
  text.classList.toggle("long", !!t && t.content.length > 120);
  text.classList.add("clamp");
  // Tiny steps (My list tasks only): "Step done ✓" first, and after the last one, "Finish the task?"
  const steps = t && String(t.id).startsWith("nb:") ? t.steps || [] : [];
  const at = steps.findIndex((s) => !s.done), step = steps[at];
  if (nowAsk && (!t || nowAsk !== t.id || at >= 0)) nowAsk = null;
  const asking = !!t && nowAsk === t.id;
  $("#nowStep").textContent = asking ? "All steps done. Finish the task?"
    : step ? `Next tiny step${steps.length > 1 ? ` (${at + 1} of ${steps.length})` : ""}: ${step.text}`
    : t && steps.length ? "All steps done ✓"
    : t ? [whenText(t), t.minutes ? `about ${t.minutes} min` : ""].filter(Boolean).join(" · ")
    : brandNew ? "Start by emptying your head into Brain dump."
    : !setAside && open.length ? "Pick anything from your list, or rest." : "";
  for (const b of ["#nowDone", "#nowPrint", "#nowSkip", "#nowTomorrow"]) { $(b).hidden = !t || (asking && b !== "#nowDone"); $(b).disabled = false; }
  $("#nowStepDone").hidden = !step || asking;
  $("#nowDone").className = step && !asking ? "plain" : "go";
  $("#nowDone").textContent = asking ? "Yes, finish it ✓" : step ? "Whole task done" : "Done ✓";
  $("#nowNotYet").hidden = !asking;
  $("#nowBreak").hidden = !aiOn() || !me?.ai_ready || !t || asking;  // "Finish the task?" is a yes/no question: nothing else beside it
  $("#nowRepeatSkip").hidden = !t || asking || !String(t.id).startsWith("nb:") || !t.repeat || !t.due;
  if (t?.due && t.due.date.slice(0, 10) > today()) $("#nowTomorrow").hidden = true;  // never moves it sooner
  $("#nowAgain").hidden = !!t || !setAside;
  $("#nowStart").hidden = !!t || !brandNew;
  $("#nowMore").hidden = !(text.scrollHeight > text.clientHeight + 2);  // a long task: the rest on request
  timerSync(t);     // the focus timer follows the card's task (FE, below)
  routineRender();  // and the routine card above it
  remindRender();   // and the reminders at the top, from the list as it is now
  // The button you pressed went away (the last "Not now", Done on the last task): focus stays on the card.
  const f = document.activeElement;
  if (hadFocus && (f === document.body || !f || (f.closest?.("#nowCard") && !f.offsetParent))) {
    const b = [...$$("#nowCard > .row button"), ...$$("#nowCard button")].find((x) => x.offsetParent && !x.disabled);
    if (b) b.focus({ preventScroll: true });
    else { $("#nowText").tabIndex = -1; $("#nowText").focus({ preventScroll: true }); }
  }
}
$("#nowMore").onclick = () => {  // it hides itself: focus goes to the task's words, all of them now (W3-07)
  $("#nowText").classList.remove("clamp"); $("#nowMore").hidden = true; focusFirst($("#nowText"));
};
$("#nowAgain").onclick = () => { skipped.clear(); renderNow(); $("#nowDone").focus(); };
$("#nowStart").onclick = () => { $("#dumpText").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" }); $("#dumpText").focus({ preventScroll: true }); };

let lastLoad = { at: 0, day: "" };
async function loadList() {
  // `since`: the routine day's start, so the routine card's "You finished N" counts that day (done_since), across midnight too
  try { listData = await api(`/list?since=${encodeURIComponent(routineDay().since)}` + (showingDone ? "&include_done=true" : "")); }
  catch (e) { $("#listBody").textContent = e.message; return; }
  if (lastLoad.day && lastLoad.day !== today()) skipped.clear();  // "Not now" means today
  lastLoad = { at: Date.now(), day: today() };
  $("#linkedErr").hidden = !listData.linked_error;
  $("#linkedErr").textContent = listData.linked_error || "";
  const ids = new Set([...listData.tasks, ...(listData.linked || [])].map((t) => t.id));
  picked = new Set([...picked].filter((id) => ids.has(id)));
  renderList();
  setWaiting(listData.marks_waiting || 0);
  if (!loadList.draftChecked) { loadList.draftChecked = true; restoreDraft(); }
  if (guide && !guide.steps.find((x) => x.key === "tasks")?.done && listData.tasks.length) loadGuide();
}

// Coming back to the app (a home-screen app resumed next morning, or another tab): fresh list, right day.
function refreshIfStale() {
  if (!me || document.visibilityState !== "visible" || !$("#mylist").classList.contains("on")) return;
  if (Date.now() - lastLoad.at > 120000 || today() !== lastLoad.day) { loadList(); loadToday(); loadGuide(); loadRoutine(); }
}
document.addEventListener("visibilitychange", refreshIfStale);
window.addEventListener("pageshow", (e) => { if (e.persisted) refreshIfStale(); });

$("#listBody").addEventListener("change", (e) => {
  if (e.target.dataset.step != null) {
    const row = e.target.closest(".task"), i = +e.target.dataset.step;
    const t = listData.tasks.find((x) => x.id === row.dataset.id);
    return tickStep(row.dataset.id, i, e.target.checked, t?.steps?.[i]?.text, e.target);
  }
  if (!e.target.classList.contains("pick")) return;
  const id = e.target.closest(".task").dataset.id;
  if (e.target.checked && picked.size >= MAX_PICK) { e.target.checked = false; return updatePicks(); }
  e.target.checked ? picked.add(id) : picked.delete(id);
  updatePicks();  // no redraw: focus stays on the box you just ticked
});
$("#listBody").addEventListener("click", async (e) => {
  if (e.target.closest("[data-pickall]")) {
    const now = today();
    for (const t of shownOpen.filter((x) => String(x.id).startsWith("nb:") && x.due && x.due.date.slice(0, 10) <= now)) {
      if (picked.size >= MAX_PICK) break;
      picked.add(t.id);
    }
    $$("#listBody .task").forEach((r) => { const b = r.querySelector(".pick"); if (b) b.checked = picked.has(r.dataset.id); });
    return updatePicks();
  }
  if (e.target.closest("[data-clear-filter]")) return $("#listFilterClear").click();
  const tagBtn = e.target.closest("[data-ftag]");
  if (tagBtn) return setFilter({ tag: tagBtn.dataset.ftag }, tagBtn.dataset.ftag);
  const row = e.target.closest(".task");
  if (!row) return;
  const id = row.dataset.id;
  const btn = e.target.closest("button");
  if (btn?.matches("[data-steps]")) {
    openSteps.has(id) ? openSteps.delete(id) : openSteps.add(id);
    listFocus = focusSpot(btn);
    renderList();
  }
  else if (btn?.matches("[data-move]")) { await moveTask(id, "tomorrow", btn); }
  else if (btn?.matches("[data-done]")) { await markDone(id, btn); }
  else if (btn?.matches("[data-reopen]")) {
    listFocus = focusSpot(btn);
    try { await api(`/list/${encodeURIComponent(id)}/reopen`, { method: "POST" }); loadList(); } catch (err) { listError(err, id); }
  }
  else if (btn?.matches("[data-edit]")) openTask(listData.tasks.find((t) => t.id === id));
});
$("#pickClear").onclick = () => { picked.clear(); $$("#listBody .pick").forEach((b) => { b.checked = false; }); updatePicks(); $("#printStickers").focus(); };

// A task ticked or deleted on another device: say so, and show the list as it is now.
const TASK_GONE = "That task was already removed.";
// A Done that changed nothing (the reply's `stale`): the task was done, skipped or moved on another device
// since this screen loaded. No "Done ✓" for it: say so, then the list is drawn again as it is now.
const DONE_STALE = "That changed on another device. Here's your list now.";
function listError(err, id) {
  toast(err.message);
  if (err.message !== TASK_GONE) return;
  if (id) picked.delete(id);
  if ($("#taskDialog").open) $("#taskDialog").close();
  loadList();
}

// Done, with a few seconds to take it back. The button can't be pressed twice while it's on its way
// (aria-disabled, not disabled, so keyboard focus stays put until the list is redrawn).
async function markDone(id, btn) {
  if (btn?.getAttribute("aria-disabled") === "true") return;
  btn?.setAttribute("aria-disabled", "true");
  if (btn) listFocus = focusSpot(btn);
  const t = [...listData.tasks, ...(listData.linked || [])].find((x) => x.id === id);
  try {  // due: the day this screen showed, so a repeat already done on another device isn't done twice
    const r = await api(`/list/${encodeURIComponent(id)}/done`, { method: "POST", json: { due: t?.due?.date?.slice(0, 10) } });
    picked.delete(id);
    if (r.stale) { toast(DONE_STALE); await loadList(); return; }
    // A linked app's repeating task has moved on there: Undo can't put its day back.
    const canUndo = r.undo != null && !(t && !String(id).startsWith("nb:") && t.due?.is_recurring);
    // Same words every time (routine_mode): always "Done ✓", never a surprise.
    snack(me?.prefs?.routine_mode ? "Done ✓" : ["Done ✓", "Nice. ✓", "That counts. ✓", "One less thing ✓"][Math.floor(Math.random() * 4)],
          canUndo ? () => undoDone(id, r.undo) : null);
    await loadList();
  } catch (err) { listError(err, id); }
  finally { btn?.removeAttribute("aria-disabled"); }
}
async function undoDone(id, undo) {
  try {
    await api(`/list/${encodeURIComponent(id)}/reopen`, { method: "POST", json: undo || {} });
    toast("Back on your list");
    await loadList();
  } catch (err) { listError(err, id); }
}

// "→ Tomorrow" (or any day) in one tap, here or in the linked app, with Undo. A move to a later day
// counts as a slide, as a → on paper does; Undo takes that back too.
const dayWords = (iso) => !iso ? "no date" : iso === today() ? "today" : iso === dayAfter(today()) ? "tomorrow"
  : new Date(iso + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
async function moveTask(id, to, btn) {
  if (btn?.getAttribute("aria-disabled") === "true") return;
  btn?.setAttribute("aria-disabled", "true");
  if (btn?.closest("#listBody")) listFocus = focusSpot(btn);
  try {
    const r = await api(`/list/${encodeURIComponent(id)}/move`, { method: "POST", json: { to } });
    if (!r.moved) return toast(r.later ? `It isn't due till ${dayWords(r.to)}, so it stays there` : `It's on ${dayWords(r.to)} already`);
    snack(`Moved to ${dayWords(r.to)}`, () => undoMove(id, r.undo));
    await loadList();
  } catch (err) { listError(err, id); }
  finally { btn?.removeAttribute("aria-disabled"); }
}
async function undoMove(id, undo) {
  try {
    const r = await api(`/list/${encodeURIComponent(id)}/move`, { method: "POST", json: undo });
    toast(`Back to ${dayWords(r.to)}`);
    await loadList();
  } catch (err) { listError(err, id); }
}

// Tick one tiny step (on a row, or Just one thing's "Step done ✓"). After the last one: finish the task?
// Quick ticks on one task go one after another, so the screen ends up showing all of them.
const stepTicks = new Map();  // task id -> its last tick on the way
function tickStep(id, ...args) {
  const go = (stepTicks.get(id) || Promise.resolve()).catch(() => null).then(() => sendStepTick(id, ...args));
  stepTicks.set(id, go);
  go.finally(() => { if (stepTicks.get(id) === go) stepTicks.delete(id); }).catch(() => {});
  return go;
}
async function sendStepTick(id, i, done, text, box = null) {
  const t = listData.tasks.find((x) => x.id === id);
  if (box) listFocus = focusSpot(box);
  try {
    const r = await api(`/list/${encodeURIComponent(id)}/steps/${i}`, { method: "PATCH", json: { done, text } });
    if (t) t.steps = r.steps;
    const all = r.steps.length > 0 && r.steps.every((s) => s.done);
    if (!box && done && all) nowAsk = id;  // Just one thing asks on the card itself
    renderList();
    if (box && done && all) snack(`All ${r.steps.length === 1 ? "steps" : r.steps.length + " steps"} done ✓`, () => markDone(id), "Finish the task");
    return r;
  } catch (err) {
    if (box) box.checked = !done;
    if (err.status === 409) { toast(err.message); loadList(); } else listError(err, id);
    return null;
  }
}

// Skip this time: a repeating task moves on to its next day. Not a win, not a slide.
async function skipRepeat(id, btn) {
  const t = listData.tasks.find((x) => x.id === id);
  if (!t?.due || btn?.getAttribute("aria-disabled") === "true") return;
  btn?.setAttribute("aria-disabled", "true");
  try {
    const r = await api(`/list/${encodeURIComponent(id)}/skip`, { method: "POST", json: { due: t.due.date.slice(0, 10) } });
    if (!r.undo) toast("It had moved on already");
    else snack(`Skipped. Next: ${dayWords(r.undo.next_due)}`, async () => {
      try { await api(`/list/${encodeURIComponent(id)}/unskip`, { method: "POST", json: r.undo }); toast(`Back to ${dayWords(r.undo.prev_due)}`); loadList(); }
      catch (err) { listError(err, id); }
    });
    await loadList();
  } catch (err) { listError(err, id); }
  finally { btn?.removeAttribute("aria-disabled"); }
}

// A message with an Undo button (or another action) that stays long enough to use it (longer with bigger text, with
// more words, and while the pointer is on it).
// After something done from the keyboard it stays until the next action or Esc, and Ctrl+Z (⌘Z on a Mac)
// undoes from anywhere on the page. Undo puts focus back where the action was.
let snackTimer = null, snackUndo = null, snackHide = null, snackKeys = false;
function snack(msg, undo, label = "Undo") {
  const box = $("#snack");
  clearTimeout(snackTimer);
  const from = document.activeElement, spot = focusSpot(from);  // where the action was done
  const goBack = () => {
    if (from?.isConnected && from.offsetParent && !box.contains(from)) return from.focus({ preventScroll: true });
    if (spot) listFocus = spot;  // the row is back once the list is drawn again after the undo
    const near = from?.closest?.(".printbar") ? $("#printStickers") : from?.closest?.("#nowCard") ? $("#nowText") : $("#listTitle");
    if (!near.matches("button") && !near.hasAttribute("tabindex")) near.tabIndex = -1;
    near.focus({ preventScroll: true });
  };
  $("#snackUndo").hidden = !undo;
  $("#snackUndo").textContent = label;
  $("#snackUndo").onclick = undo ? () => { hide(true); undo(); } : null;
  snackUndo = undo && label === "Undo" ? $("#snackUndo").onclick : null;  // Ctrl+Z: an undo, never "Finish the task"
  box.classList.add("show");
  $("#snackText").textContent = "";
  const keys = snackUndo ? `<span class="sr-only"> (Undo: ${keysMac() ? "⌘" : "Ctrl"}+Z)</span>` : "";
  requestAnimationFrame(() => { $("#snackText").innerHTML = esc(msg) + keys; placeToast(); });  // said again even if it's the same words
  placeToast();
  function hide(now) {
    if (!now && (box.contains(document.activeElement) || box.matches(":hover"))) { snackTimer = setTimeout(hide, 2000); return; }
    clearTimeout(snackTimer);
    if (box.contains(document.activeElement)) goBack();
    box.classList.remove("show");
    if (snackHide === hide) { snackUndo = null; snackHide = null; }
    placeToast();
  }
  snackHide = hide;
  hide.waits = snackKeys;
  // 6 s (10 s with bigger text) reads a short one and reaches Undo; a longer one (a bulk action naming a task that
  // didn't change, and why) gets 60 ms more for each character past the first 40, as a toast reads.
  if (!snackKeys) snackTimer = setTimeout(hide, (["large", "xlarge"].includes(me?.prefs?.text_size) ? 10000 : 6000)
    + 60 * Math.max(0, String(msg).length - 40));
}
// Was the last thing done with the keyboard (Enter, Space) or a pointer (tap, click)?
document.addEventListener("keydown", (e) => { if (!["Shift", "Control", "Alt", "Meta"].includes(e.key)) snackKeys = true; }, true);
document.addEventListener("pointerdown", () => { snackKeys = false; }, true);
// A message that waits (from the keyboard) goes on the next action anywhere else, or with Esc.
document.addEventListener("click", (e) => { if (snackHide?.waits && !e.target.closest?.("#snack")) snackHide(true); }, true);
document.addEventListener("keydown", (e) => {
  if (!snackHide || e.defaultPrevented || $$("dialog[open]").length) return;
  if (e.key === "Escape") return snackHide(true);
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "z" && snackUndo && !keysTyping(e.target)
      && $("#mylist").classList.contains("on")) {  // only an Undo you can see
    e.preventDefault();
    snackUndo();
  }
});

$("#nowDone").onclick = () => $("#nowCard").dataset.id && markDone($("#nowCard").dataset.id, $("#nowDone"));
$("#nowStepDone").onclick = async () => {
  const id = $("#nowCard").dataset.id, b = $("#nowStepDone");
  const t = listData.tasks.find((x) => x.id === id), i = (t?.steps || []).findIndex((s) => !s.done);
  if (i < 0 || b.getAttribute("aria-disabled") === "true") return;
  b.setAttribute("aria-disabled", "true");
  const text = t.steps[i].text;
  try {
    const r = await tickStep(id, i, true, text);
    if (!r) return;
    snack("Step done ✓", () => tickStep(id, i, false, text));
    if (nowAsk === id) $("#nowDone").focus();  // "All steps done. Finish the task?" Yes is right there
  } finally { b.removeAttribute("aria-disabled"); }
};
$("#nowNotYet").onclick = () => { nowAsk = null; renderNow(); $("#nowDone").focus(); };
$("#nowTomorrow").onclick = () => $("#nowCard").dataset.id && moveTask($("#nowCard").dataset.id, "tomorrow", $("#nowTomorrow"));
$("#nowRepeatSkip").onclick = () => $("#nowCard").dataset.id && skipRepeat($("#nowCard").dataset.id, $("#nowRepeatSkip"));
$("#nowSkip").onclick = () => { skipped.add($("#nowCard").dataset.id); listData.one_thing = null; renderNow(); };
$("#nowPrint").onclick = () => printFrom("/print/focus", { task_id: $("#nowCard").dataset.id }, $("#nowPrint"));
$("#printTodayList").onclick = () => printFrom("/print/today", { source: "manual" }, $("#printTodayList"));

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
    loadRoutine();  // today's label is made: the routine card steps aside
    if (body.task_ids && [r, ...(r.also || [])].every((x) => x.status === "printed")) {
      picked.clear();  // printed for real: un-tick them, so the next press doesn't print them again
      $$("#listBody .pick").forEach((b) => { b.checked = false; });
      updatePicks();
    }
    if (gone) { for (const id of r.missing) picked.delete(id); loadList(); }
  } catch (e) {
    showResultError(box, errText(e));
    if (/already removed/.test(e.message)) { picked.clear(); loadList(); }
  }
  finally { done(); }
}
for (const [b, path] of [["#printStickers", "/print/stickers"], ["#printStrips", "/print/strips"]]) {
  $(b).onclick = () => { const body = printPicks(); body ? printFrom(path, body, $(b)) : toast("Nothing shown to print. Show everything first."); };
}

// Bulk actions on the picked tasks: Done and → Tomorrow (one Undo for all of them), Tag… and Delete….
// One task at a time, through the same calls as a single tap, so linked tasks go to their own app.
const BULK = ["#bulkDone", "#bulkTomorrow", "#bulkTag", "#bulkDelete", "#pickClear", "#printStickers", "#printStrips"];
function pickedTasks() {
  const all = [...listData.tasks, ...(listData.linked || [])];
  return [...picked].map((id) => all.find((t) => t.id === id)).filter((t) => t && !t.done);
}
async function eachPicked(verb, tasks, fn) {
  const ok = [], failed = [];
  BULK.forEach((s) => $(s).setAttribute("aria-disabled", "true"));
  try {
    for (const [i, t] of tasks.entries()) {
      $("#pickCount").textContent = `${verb} ${i + 1} of ${tasks.length}…`;
      try { ok.push([t, await fn(t)]); }
      catch (e) { failed.push([t, e]); if (!e.status) break; }  // offline: stop, don't try the rest
    }
  } finally { BULK.forEach((s) => $(s).removeAttribute("aria-disabled")); }
  for (const [t] of ok) picked.delete(t.id);
  updatePicks();  // the bar says what's picked again, not "3 of 5…"
  const missed = tasks.length - ok.length;  // failed, or not tried after an offline one
  if (failed.length && !ok.length) {  // nothing changed: say why (some did: the message about them says which didn't)
    toast(didntChange(failed, missed), { error: true });
    if (failed.some(([, x]) => x.message === TASK_GONE)) loadList();
  }
  ok.missed = failed.length ? ` · ${didntChange(failed, missed)}` : "";
  return ok;
}
// “Pick up prescription” didn't change: That task was already removed. (Or “…” and 2 more, of n that didn't.)
const didntChange = (failed, n = failed.length) =>
  `“${shortName(failed[0][0])}”${n > 1 ? ` and ${n - 1} more` : ""} didn't change: ${errText(failed[0][1])}`;
// One Undo for a whole batch, a task at a time: says how many went back, and names one that didn't and why.
async function undoPicked(pairs, fn, words, of = pairs.length) {
  const bad = [];
  let back = 0;
  for (const [t, r] of pairs) {
    try { await fn(t, r); back++; } catch (e) { bad.push([t, e]); if (!e.status) break; }  // offline: stop, don't try the rest
  }
  if (!bad.length) toast(`${words}${back < of ? ` (${back} of ${of})` : ""}`);
  else toast(`${back ? `${words} (${back} of ${of}) · ` : ""}${didntChange(bad, pairs.length - back)}`, { error: true });
  loadList();
}
const bulkBusy = () => $("#bulkDone").getAttribute("aria-disabled") === "true";
$("#bulkDone").onclick = async () => {
  if (bulkBusy() || !picked.size) return;
  const ok = await eachPicked("Finishing", pickedTasks(), (t) => api(`/list/${encodeURIComponent(t.id)}/done`, { method: "POST", json: { due: t.due?.date?.slice(0, 10) } }));
  if (!ok.length) return;
  // Ones that changed on another device weren't done here: they aren't counted as done (see DONE_STALE).
  const done = ok.filter(([, r]) => !r.stale), stale = ok.length - done.length;
  const undo = done.filter(([t, r]) => r.undo != null && !(!String(t.id).startsWith("nb:") && t.due?.is_recurring));
  if (!done.length) toast(`${stale === 1 ? "That" : `Those ${stale}`} changed on another device${ok.missed}. Here's your list now.`);
  else snack(`Done ${done.length} ✓${stale ? ` · ${stale} changed on another device` : ""}${ok.missed}`, undo.length ? () => undoPicked(undo,
    (t, r) => api(`/list/${encodeURIComponent(t.id)}/reopen`, { method: "POST", json: r.undo || {} }), "Back on your list", done.length) : null);
  await loadList();
  $("#printStickers").focus();
};
$("#bulkTomorrow").onclick = async () => {
  if (bulkBusy() || !picked.size) return;
  // Like a row's →, it never moves a task sooner: one that isn't due yet stays where it is (and picked).
  const all = pickedTasks(), now = today();
  const tasks = all.filter((t) => !t.due || t.due.date.slice(0, 10) <= now);
  const waits = all.length - tasks.length, notYet = waits ? ` · ${waits} ${waits === 1 ? "wasn't" : "weren't"} due yet` : "";
  if (!tasks.length) return toast(waits === 1 ? "That one isn't due yet, so it stays where it is." : "None of them are due yet, so they stay where they are.");
  const ok = await eachPicked("Moving", tasks, (t) => api(`/list/${encodeURIComponent(t.id)}/move`, { method: "POST", json: { to: "tomorrow" } }));
  if (!ok.length) return;
  const moved = ok.filter(([, r]) => r.moved);
  // Where they went, as a row's → says it: a weekday repeat moved on a Friday goes to Monday.
  const days = [...new Set(moved.map(([, r]) => r.to))];
  const where = days.length <= 2 ? days.map(dayWords).join(" and ") : "later days";
  if (moved.length) snack(`Moved ${moved.length} to ${where}${notYet}${ok.missed}`, () => undoPicked(moved,
    (t, r) => api(`/list/${encodeURIComponent(t.id)}/move`, { method: "POST", json: r.undo }), "Back where they were"));
  else toast(`They're on tomorrow already${notYet}${ok.missed}`);
  await loadList();
  $("#printStickers").focus();
};
// Closed without doing anything (Esc, Cancel, Close): focus goes back to the button that opened it (W3-08).
// After Delete or a tag, the picks change, so it goes to Stickers (bulkOpener is cleared first).
let bulkOpener = null;
function bulkDialog(title, body, buttons) {
  bulkOpener = document.activeElement;
  $("#bulkTitle").textContent = title;
  $("#bulkBody").innerHTML = body;
  $("#bulkButtons").innerHTML = buttons;
  $("#bulkDialog").showModal();
}
$("#bulkDialog").addEventListener("close", () => {
  const o = bulkOpener; bulkOpener = null;
  if ($("#taskDialog").open) return;
  (o?.isConnected && o.offsetParent && o.getAttribute("aria-disabled") !== "true" ? o : $("#printStickers")).focus();
});
closeOnBackdrop($("#bulkDialog"));
$("#bulkTag").onclick = () => {
  if (bulkBusy() || !picked.size) return;
  const tasks = pickedTasks(), mine = tasks.filter((t) => String(t.id).startsWith("nb:"));
  const on = [...new Set(mine.flatMap((t) => t.labels || []))];
  const away = tasks.length - mine.length;
  bulkDialog(mine.length ? `Tag ${plural(mine.length, "task")}` : "Tags are for My list tasks",
    (!mine.length ? `<p class="muted">Change these in ${esc(listData.linked_name || "your linked app")}.</p>`
      : `<p class="muted">Add one:</p><div class="chips">${listData.tags.map((g) => `<button type="button" class="tc-${esc(g.color)}" data-btag="${esc(g.name)}" data-add="1"><span class="tdot" aria-hidden="true"></span>${esc(g.name)}</button>`).join("") || `<span class="muted">No tags yet. Add some in Settings.</span>`}</div>`
        + (on.length ? `<p class="muted">Or take one off:</p><div class="chips">${on.map((n) => `<button type="button" data-btag="${esc(n)}" aria-label="Take #${esc(n)} off">✕ ${esc(n)}</button>`).join("")}</div>` : "")
        + (away ? `<p class="muted">${away === 1 ? "1 is" : `${away} are`} in ${esc(listData.linked_name || "your linked app")}: tag ${away === 1 ? "it" : "them"} there.</p>` : "")),
    `<button class="plain" id="bulkCancel">Close</button>`);
  $("#bulkBody").classList.add("bulktags");
  $("#bulkCancel").onclick = () => $("#bulkDialog").close();
  $("#bulkBody").onclick = async (e) => {
    const b = e.target.closest("[data-btag]");
    if (!b || b.getAttribute("aria-disabled") === "true") return;
    const name = b.dataset.btag, adding = !!b.dataset.add;
    const change = mine.filter((t) => (t.labels || []).includes(name) !== adding);
    $$("#bulkBody [data-btag]").forEach((x) => x.setAttribute("aria-disabled", "true"));
    bulkOpener = null; $("#bulkDialog").close();
    if (!change.length) return toast(adding ? `They all have #${name} already` : `None of them has #${name}`);
    const ok = await eachPicked(adding ? "Tagging" : "Untagging", change, (t) => api(`/list/${encodeURIComponent(t.id)}`, { method: "PATCH",
      json: { tags: adding ? [...(t.labels || []), name] : (t.labels || []).filter((x) => x !== name) } }));
    for (const t of mine) picked.add(t.id);  // still picked: tagging is often the first of a few steps
    updatePicks();
    if (!ok.length) return;
    toast(`${adding ? "Added" : "Took"} #${name} ${adding ? "to" : "off"} ${plural(ok.length, "task")}${ok.missed}`);
    await loadList();
  };
};
$("#bulkDelete").onclick = () => {
  if (bulkBusy() || !picked.size) return;
  const tasks = pickedTasks(), away = tasks.filter((t) => !String(t.id).startsWith("nb:")).length;
  const n = tasks.length;
  bulkDialog(`Delete ${plural(n, "task")}? This can't be undone.`,
    `<ul class="bulklist">${tasks.map((t) => `<li>${esc(shortName(t))}</li>`).join("")}</ul>`
      + (away ? `<p class="muted">${away === 1 ? "1 is" : `${away} are`} in ${esc(listData.linked_name || "your linked app")}: deleted there too.</p>` : ""),
    `<button class="go warn" id="bulkDelGo">Delete ${n}</button><button class="plain" id="bulkCancel">Cancel</button>`);
  $("#bulkBody").classList.remove("bulktags");
  $("#bulkCancel").onclick = () => $("#bulkDialog").close();
  $("#bulkCancel").focus();  // the safe choice has focus
  $("#bulkDelGo").onclick = async () => {
    bulkOpener = null; $("#bulkDialog").close();
    const ok = await eachPicked("Deleting", tasks, (t) => api(`/list/${encodeURIComponent(t.id)}?confirm=true`, { method: "DELETE" }));
    if (!ok.length) return;
    toast(`Deleted ${plural(ok.length, "task")}${ok.missed}`);
    await loadList();
  };
};
$("#showDone").onclick = () => {
  showingDone = !showingDone;
  $("#showDone").textContent = showingDone ? "Hide done" : "Show done";
  $("#showDone").setAttribute("aria-pressed", String(showingDone));
  loadList();
};

// Brain dump. An unsent draft is kept on this device for the person who typed it (until it's added,
// they sign out, someone else signs in here, or a day goes by), so a phone that drops the page in the
// background, or a session that ends, doesn't lose it.
const draftKey = () => (me ? `nb.dumpDraft:${me.username}` : "");
const DRAFT_KEEP = 24 * 3600 * 1000;
function saveDraft() {
  try {
    const v = $("#dumpText").value;
    if (draftKey()) v.trim() ? localStorage.setItem(draftKey(), JSON.stringify({ text: v, at: Date.now() })) : localStorage.removeItem(draftKey());
  } catch {}
}
function restoreDraft() {
  try {
    const raw = draftKey() && localStorage.getItem(draftKey());
    if (!raw) return;
    let v = raw;  // older copies kept the words only
    if (raw.startsWith("{")) { try { const d = JSON.parse(raw); v = Date.now() - (+d.at || 0) < DRAFT_KEEP ? String(d.text || "") : ""; } catch {} }
    if (!v) return forgetDraft();  // a day old: gone
    if (!$("#dumpText").value.trim()) { $("#dumpText").value = v; runPreview(); }
  } catch {}
}
function forgetDraft() {
  try { if (draftKey()) localStorage.removeItem(draftKey()); } catch {}
}
let draftTimer = null;
$("#dumpText").addEventListener("input", () => { clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 400); });
$("#dumpText").addEventListener("keydown", (e) => {  // Ctrl+Enter (⌘+Enter on a Mac) adds it
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); $("#dumpGo").click(); }
});

// "2 today, 1 tomorrow, 2 someday": where the new tasks went.
function dumpSummary(added) {
  const now = today(), n = { today: 0, tomorrow: 0, later: 0, someday: 0 };
  for (const t of added) {
    const d = t.due?.date?.slice(0, 10);
    n[!d ? "someday" : d <= now ? "today" : d === dayAfter(now) ? "tomorrow" : "later"]++;
  }
  return Object.entries(n).filter(([, v]) => v).map(([k, v]) => `${v} ${k === "later" ? "coming up" : k}`).join(", ");
}

$("#dumpGo").onclick = async () => {
  const text = $("#dumpText").value;
  if (!text.trim()) return toast("Nothing to add yet");
  $("#dumpGo").disabled = true;
  $("#dumpProblems").innerHTML = "";
  $("#dumpMsg").className = "muted";
  try {
    const r = await api("/list/dump", { method: "POST", json: { text } });
    $("#dumpText").value = r.skipped_text || "";  // lines past the first 200 stay in the box, for next time
    saveDraft();
    const toApp = r.to && r.to !== "My list";
    const where = toApp ? ` to ${r.to}` : r.added.length ? `: ${dumpSummary(r.added)}` : "";
    const rest = r.skipped_text ? r.skipped_text.split("\n").filter((l) => l.trim()).length : 0;
    $("#dumpMsg").textContent = [
      `Added ${r.added.length}${where}.` + (rest || r.error ? "" : " Your head has more room now."),
      rest ? `${rest} more line${rest === 1 ? " is" : "s are"} still in the box: 200 at a time, so add ${rest === 1 ? "it" : "them"} next.` : "",
      (r.left_out || []).length ? `Left out ${r.left_out.length} line${r.left_out.length === 1 ? "" : "s"} ticked already ([x]).` : "",
      ...(r.notes || []), r.error || ""].filter(Boolean).join(" ");
    if (r.error) $("#dumpMsg").className = "err";
    runPreview();
    await loadList();
    if (!toApp) flashRows(r.added.map((t) => t.id));
  } catch (e) {
    const problems = e.body?.problems || [];
    $("#dumpMsg").className = "err";
    $("#dumpMsg").textContent = problems.length ? `Nothing was added yet. Fix ${problems.length === 1 ? "this line" : "these lines"}, then add again:` : errText(e);
    $("#dumpProblems").innerHTML = problems.map((p) => `<li><b>Line ${p.line}:</b> ${esc(p.problem)} <span class="muted">(“${esc(p.text)}”)</span></li>`).join("");
    loadList();  // in case anything changed anyway
  }
  finally { $("#dumpGo").disabled = false; }
};
// Brain dump's live preview: what each line becomes (day, time, tags, how long, repeat), before anything is
// added. The server only parses (POST /list/dump?preview=1); an answer for older text is thrown away.
let previewTimer = null, previewN = 0;
$("#dumpText").addEventListener("input", () => { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, 450); });
try { if (localStorage.getItem("nb.dumpPreview") === "closed") $("#dumpPreview").open = false; } catch {}
$("#dumpPreview").addEventListener("toggle", () => {
  try { $("#dumpPreview").open ? localStorage.removeItem("nb.dumpPreview") : localStorage.setItem("nb.dumpPreview", "closed"); } catch {}
});
const PREVIEW_ROWS = 60;
function previewChips(x, r) {
  const c = [];
  if (x.due_date) c.push(["", "📅 " + dayWords(x.due_date)]);
  if (x.due_time) c.push(["", "🕒 " + fmtClock(x.due_time)]);
  if (x.repeat) c.push(["", "↻ " + x.repeat]);
  if (x.minutes) c.push(["", `~${x.minutes}m`]);
  if (x.priority >= 3) c.push(["", "! urgent"]);
  return c.map(([k, s]) => `<span class="pchip ${k}">${esc(s)}</span>`).join("")
    + (x.tags || []).map((n) => (r.over_tags || []).includes(n) ? `<span class="pchip warn">#${esc(n)} stays in the words (30 tags is the most)</span>`
      : tagChip(n) + ((r.new_tags || []).includes(n) ? `<span class="pchip">new tag</span>` : "")).join("");
}
async function runPreview() {
  const text = $("#dumpText").value, n = ++previewN;
  if (!text.trim()) { $("#dumpPreview").hidden = true; $("#dumpPreviewSay").textContent = ""; return; }
  let r;
  try { r = await api("/list/dump?preview=1", { method: "POST", json: { text } }); }
  catch { if (n === previewN) $("#dumpPreview").hidden = true; return; }  // the preview is a help, never an error
  if (n !== previewN || !me) return;  // typed more since, or signed out
  const ok = r.lines.filter((x) => !x.problem && !x.ticked);
  const bad = r.lines.filter((x) => x.problem).length, left = r.lines.filter((x) => x.ticked).length + r.dropped.length;
  const where = r.to && r.to !== "My list" ? ` to ${r.to}` : "";
  const sum = ok.length ? dumpSummary(ok.map((x) => ({ due: x.due_date ? { date: x.due_date } : null }))) : "";
  const head = [ok.length ? `Adds ${plural(ok.length, "task")}${where}${sum && !where ? `: ${sum}` : ""}` : "Nothing to add yet",
    bad ? `${plural(bad, "line")} to fix` : "", left ? `${left} left out` : ""].filter(Boolean).join(" · ");
  $("#dumpPreviewHead").textContent = head;
  if ($("#dumpPreviewSay").textContent !== head) $("#dumpPreviewSay").textContent = head;  // said when it changes, not on every key
  $("#dumpLines").innerHTML = r.lines.slice(0, PREVIEW_ROWS).map((x) => x.problem
      ? `<li class="bad"><span class="ln">${x.line}</span>${esc(x.problem)}. <span>(“${esc(x.text)}”)</span></li>`
      : x.ticked ? `<li class="left"><span class="ln">${x.line}</span>${esc(x.content)} <span class="pchip">ticked already: left out</span></li>`
      : `<li><span class="ln">${x.line}</span><b>${esc(x.content)}</b><div class="pchips">${previewChips(x, r)}</div></li>`).join("")
    + (r.lines.length > PREVIEW_ROWS ? `<li class="left">…and ${plural(r.lines.length - PREVIEW_ROWS, "more line")}</li>` : "");
  const drop = r.dropped.map((d) => `“${d.text}”`).join(", ");
  $("#dumpDropped").textContent = [
    r.dropped.length ? `${r.dropped.length === 1 ? "1 line has" : `${r.dropped.length} lines have`} no task words, so ${r.dropped.length === 1 ? "it'll be" : "they'll be"} left out: ${drop}.` : "",
    r.more ? `${plural(r.more, "more line")} will wait in the box: 200 at a time.` : "", ...(r.notes || [])].filter(Boolean).join(" ");
  $("#dumpPreview").hidden = false;
}

// New rows light up for a moment, so they're easy to find.
function flashRows(ids) {
  const want = new Set(ids);
  $$("#listBody .task").filter((r) => want.has(r.dataset.id)).forEach((r) => {
    r.classList.add("fresh");
    setTimeout(() => r.classList.remove("fresh"), 2500);
  });
}

// edit dialog (and "+ Add task", which opens it empty: editing.id is null until it's saved)
let editing = null;
let editSteps = [];  // the tiny steps as the dialog shows them: [{text, done}]
const MAX_STEPS = 12;  // the server keeps at most 12
function openTask(t) {
  editing = t;
  const creating = !t.id;
  $("#tdTitle").textContent = creating ? "New task" : "Edit task";
  $("#tdContent").value = t.content;
  $("#tdDate").value = t.due ? t.due.date.slice(0, 10) : "";
  $("#tdTime").value = t.due && t.due.date.length > 10 ? t.due.date.slice(11, 16) : "";
  $("#tdMinutes").value = t.minutes || "";
  $("#tdRepeat").value = t.repeat || "";
  $("#tdPriority").value = String(Math.min(4, Math.max(1, t.priority || 1)));
  $("#tdNotes").value = t.description || "";
  editSteps = (t.steps || []).map((s) => ({ text: s.text, done: !!s.done }));
  $("#tdStepNew").value = "";
  drawSteps();
  const on = new Set(t.labels || []);
  // Your tags, plus any this task still has whose tag was removed, so saving never drops one quietly.
  const names = [...new Set([...listData.tags.map((g) => g.name), ...(t.labels || [])])];
  $("#tdTags").innerHTML = names.map((g) => `<button type="button" data-tag="${esc(g)}" class="${on.has(g) ? "on" : ""}">${esc(g)}</button>`).join("")
    || `<span class="muted">No tags yet. Add some in Settings.</span>`;
  $("#tdDelete").hidden = creating;
  $("#tdSkipRepeat").hidden = creating || !t.repeat || !t.due;
  const elsewhere = creating && me?.prefs?.new_tasks_to === "linked" && listData.linked_name;
  $("#tdWhere").hidden = !elsewhere;
  $("#tdWhere").textContent = elsewhere ? `This goes on My list. (Brain dump sends new tasks to ${listData.linked_name}.)` : "";
  dayChips();
  focusLabel();
  $("#taskDialog").showModal();
  fitSteps();
  if (creating) $("#tdContent").focus();
}
function openNewTask() {  // a tag that's being shown starts ticked, so the new task shows up in it
  openTask({ id: null, content: "", description: "", priority: 1, due: null, labels: listData.tags.some((g) => g.name === listFilter.tag) ? [listFilter.tag] : [], steps: [] });
}
$("#addTask").onclick = openNewTask;

// The tiny steps: tick, edit, ↑/↓, ✕, and "Add a step". Saved with the rest of the task.
function drawSteps(focus = null) {
  const n = editSteps.length;
  $("#tdStepList").innerHTML = editSteps.map((s, i) => `<li data-i="${i}" class="${s.done ? "done" : ""}">
      <input type="checkbox" data-sdone ${s.done ? "checked" : ""} aria-label="Step ${i + 1} done">
      <textarea data-stext rows="1" maxlength="200" aria-label="Step ${i + 1}">${esc(s.text)}</textarea>
      <button type="button" class="plain" data-smove="-1" aria-label="Move step ${i + 1} up"${i === 0 ? " disabled" : ""}>↑</button>
      <button type="button" class="plain" data-smove="1" aria-label="Move step ${i + 1} down"${i === n - 1 ? " disabled" : ""}>↓</button>
      <button type="button" class="plain" data-sdel aria-label="Remove step ${i + 1}">✕</button></li>`).join("");
  $("#tdStepList").hidden = !n;
  fitSteps();
  const full = n >= MAX_STEPS;
  $("#tdStepNew").disabled = full; $("#tdStepAdd").disabled = full; $("#tdStepsFull").hidden = !full;
  if (focus) {  // focus.sel: where focus goes in that step, first choice first (a disabled ↑ falls back to ↓)
    const li = $(`#tdStepList li[data-i="${focus.i}"]`);
    const el = li && ([...(focus.sel || []), "[data-sdel]"].map((s) => li.querySelector(s)).find((b) => b && !b.disabled));
    (el || ($("#tdStepNew").disabled ? $("#tdStepList input") : $("#tdStepNew")))?.focus();
  }
  focusLabel();
}
// Each step's box grows to show all of it (once the dialog is open and the boxes have a size).
const fitSteps = () => { if ($("#taskDialog").open) $$("#tdStepList textarea").forEach(fitText); };
window.addEventListener("resize", fitSteps);  // turning the phone, or a new text size: the boxes follow
function setEditSteps(texts, done = false) {  // fill the step list from elsewhere (e.g. "Make it smaller")
  editSteps = texts.map((x) => String(x).trim()).filter(Boolean).slice(0, MAX_STEPS).map((text) => ({ text, done }));
  drawSteps();
}
function addSteps(text) {
  const room = MAX_STEPS - editSteps.length;
  const lines = String(text).split(/\r?\n/).map((x) => x.replace(/^\s*(?:[-*•]|\d{1,2}[.)])\s+/, "").trim()).filter(Boolean);
  editSteps.push(...lines.slice(0, Math.max(0, room)).map((t) => ({ text: t.slice(0, 200), done: false })));
  if (lines.length > room) toast(`${MAX_STEPS} steps is the most, so ${plural(lines.length - room, "line")} didn't fit.`);
  return lines.length > 0;
}
$("#tdStepAdd").onclick = () => {
  if (!$("#tdStepNew").value.trim()) { toast("Type a step first"); return $("#tdStepNew").focus(); }
  addSteps($("#tdStepNew").value);
  $("#tdStepNew").value = "";
  drawSteps({ i: -1 });
  if (!$("#tdStepNew").disabled) $("#tdStepNew").focus();
};
$("#tdStepNew").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); if ($("#tdStepNew").value.trim()) $("#tdStepAdd").click(); }
});
$("#tdStepNew").addEventListener("paste", (e) => {  // a pasted list becomes one step per line
  const text = e.clipboardData?.getData("text") || "";
  if (!/\n/.test(text.trim())) return;
  e.preventDefault();
  addSteps(text);
  drawSteps({ i: -1 });
  if (!$("#tdStepNew").disabled) $("#tdStepNew").focus();
});
$("#tdStepList").addEventListener("input", (e) => {
  const li = e.target.closest("li"), i = +li.dataset.i;
  if (e.target.matches("[data-stext]")) { editSteps[i].text = e.target.value.replace(/\s*\n\s*/g, " "); fitText(e.target); }
  if (e.target.matches("[data-sdone]")) { editSteps[i].done = e.target.checked; li.classList.toggle("done", e.target.checked); }
});
$("#tdStepList").addEventListener("keydown", (e) => {  // Enter in a step goes on to "Add a step", not Save
  if (e.key === "Enter" && e.target.matches("[data-stext]") && !e.isComposing) { e.preventDefault(); $("#tdStepNew").focus(); }
});
$("#tdStepList").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const i = +b.closest("li").dataset.i;
  if (b.matches("[data-sdel]")) {
    editSteps.splice(i, 1);
    drawSteps(editSteps.length ? { i: Math.min(i, editSteps.length - 1), sel: ["[data-sdel]"] } : { i: -1 });
  } else if (b.dataset.smove) {
    const dir = Number(b.dataset.smove), j = i + dir;
    if (j < 0 || j >= editSteps.length) return;
    [editSteps[i], editSteps[j]] = [editSteps[j], editSteps[i]];
    drawSteps({ i: j, sel: [`[data-smove="${dir}"]`, `[data-smove="${-dir}"]`] });  // keep pressing ↓ to carry it down
  }
});
// The steps as they'd be saved: what's typed in "Add a step" counts too, so it's never lost.
function stepsNow() {
  const out = editSteps.map((s) => ({ text: s.text.trim().slice(0, 200), done: !!s.done })).filter((s) => s.text);
  const typed = $("#tdStepNew").value.trim();
  if (typed && out.length < MAX_STEPS) out.push({ text: typed.slice(0, 200), done: false });
  return out;
}

// Day chips: Today, Tomorrow, This weekend, Next week, No date. They set the Day field.
function quickDay(kind) {
  const now = today(), wd = new Date(now + "T12:00").getDay();  // 0 Sunday … 6 Saturday
  return kind === "today" ? now : kind === "tomorrow" ? dayAfter(now)
    : kind === "weekend" ? (wd === 6 || wd === 0 ? now : dayAfter(now, 6 - wd))  // Saturday, or today at the weekend
    : kind === "nextweek" ? dayAfter(now, (8 - wd) % 7 || 7) : "";                // next Monday
}
function dayChips() {
  const v = $("#tdDate").value;
  $$("#tdDays [data-day]").forEach((b) => {
    const d = quickDay(b.dataset.day);
    b.setAttribute("aria-pressed", String(d === v));
    b.title = d ? new Date(d + "T12:00").toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }) : "";
    if (d && b.dataset.day !== "today" && b.dataset.day !== "tomorrow") b.setAttribute("aria-label", `${b.textContent}, ${b.title}`);
  });
  // Clearing the day of a repeating task ends the repeat (a repeat needs a day): say so before Save.
  const ends = !!editing?.id && !!editing.due && !v && !!(editing.repeat || $("#tdRepeat").value);
  const msg = ends ? "Clearing the day stops it repeating. Pick a day to keep it going." : "";
  if ($("#tdRepeatWarn").textContent !== msg) $("#tdRepeatWarn").textContent = msg;
}
$("#tdDays").onclick = (e) => {
  const b = e.target.closest("[data-day]");
  if (!b) return;
  $("#tdDate").value = quickDay(b.dataset.day);
  $("#tdDate").dispatchEvent(new Event("input", { bubbles: true }));  // the same as typing it: chips, warning, "Save &"
};
for (const id of ["#tdDate", "#tdRepeat"]) for (const ev of ["input", "change"]) $(id).addEventListener(ev, dayChips);
// Skip this time (a repeating task): unsaved changes are saved first, then it moves on to its next day.
$("#tdSkipRepeat").onclick = async () => {
  const id = editing?.id;
  if (!id || tdBusy()) return;
  if (!$("#tdContent").value.trim()) return $("#taskForm").reportValidity();
  try { await tdHold(saveEditing); } catch (err) { return listError(err, id); }
  $("#taskDialog").close();
  await loadList();
  skipRepeat(id, null);
};
$("#tdTags").onclick = (e) => {
  const b = e.target.closest("[data-tag]");
  if (!b) return;
  b.classList.toggle("on");
  if (b.dataset.tag === "urgent" && b.classList.contains("on") && +$("#tdPriority").value < 3) $("#tdPriority").value = "3";  // the urgent tag is urgent
  else if (b.dataset.tag === "urgent" && !b.classList.contains("on") && $("#tdPriority").value === "3") {
    // Taking it off takes back the urgency it gave (as the server does), not a "most urgent" set on purpose.
    const was = Math.min(4, Math.max(1, editing?.priority || 1));
    if (was < 3 || (editing?.labels || []).includes("urgent")) $("#tdPriority").value = String(was < 3 ? was : 1);
  }
  focusLabel();
};
$("#tdCancel").onclick = () => { editing = null; };

// What the form changes, compared with the task as it was opened: only those fields are sent, so
// saving never quietly changes something you didn't touch.
function editBody() {
  const t = editing, body = {};
  let day = $("#tdDate").value;
  // A time or repeat needs a day: today, as Brain dump does. (Clearing the day of a task that had
  // one takes it off the calendar, time and repeat too.)
  if (!day && !t.due && ($("#tdTime").value || $("#tdRepeat").value)) day = today();
  const was = {
    content: t.content, description: t.description || "", due_date: t.due ? t.due.date.slice(0, 10) : "",
    due_time: t.due && t.due.date.length > 10 ? t.due.date.slice(11, 16) : "", repeat: t.repeat || "",
    minutes: String(t.minutes || ""), priority: String(t.priority || 1),
  };
  const now = {
    content: $("#tdContent").value.trim(), description: $("#tdNotes").value, due_date: day,
    due_time: day ? $("#tdTime").value : "", repeat: day ? $("#tdRepeat").value : "",
    minutes: $("#tdMinutes").value, priority: $("#tdPriority").value,
  };
  for (const k of Object.keys(now)) {
    if (now[k] === was[k]) continue;
    body[k] = k === "minutes" ? (now[k] ? Number(now[k]) : null) : k === "priority" ? Number(now[k])
      : ["due_date", "due_time", "repeat"].includes(k) ? (now[k] || null) : now[k];
  }
  const tags = $$("#tdTags [data-tag].on").map((b) => b.dataset.tag);
  if ([...tags].sort().join("\n") !== [...(t.labels || [])].sort().join("\n")) body.tags = tags;
  // Urgent taken off: the urgency shown here is the one meant (the server would take back what the tag gave).
  if (body.tags && (t.labels || []).includes("urgent") && !tags.includes("urgent")) body.priority = Number(now.priority);
  const steps = stepsNow();
  if (JSON.stringify(steps) !== JSON.stringify((t.steps || []).map((s) => ({ text: s.text, done: !!s.done })))) body.steps = steps;
  return body;
}
async function saveEditing() {
  const body = editBody();
  if (!editing.id) {  // + Add task: made now, on My list (a tag it started with counts as chosen)
    const task = await api("/list", { method: "POST", json: { ...body, content: $("#tdContent").value.trim(),
      tags: $$("#tdTags [data-tag].on").map((b) => b.dataset.tag), steps: stepsNow() } });
    editing = { ...editing, ...task };  // as saved: a dialog that stays open (Break it down) has nothing unsaved now
    return { ...body, created: task };
  }
  if (Object.keys(body).length) {
    const task = await api(`/list/${encodeURIComponent(editing.id)}`, { method: "PATCH", json: body });
    if (task?.id) editing = { ...editing, ...task };
  }
  return body;
}
// Save, "Save & print just this", Break it down and Skip this time all save first. While one is
// saving, none of them can be pressed (aria-disabled: the page ignores clicks on it), so a double tap
// never adds the task twice or prints it twice.
const TD_SAVERS = ["#tdSave", "#tdFocus", "#tdBreak", "#tdSkipRepeat"];
const tdBusy = () => TD_SAVERS.some((s) => $(s).getAttribute("aria-disabled") === "true");
async function tdHold(fn) {
  TD_SAVERS.forEach((s) => $(s).setAttribute("aria-disabled", "true"));
  try { return await fn(); } finally { TD_SAVERS.forEach((s) => $(s).removeAttribute("aria-disabled")); }
}
// Unsaved words, steps or settings in the task dialog (a new task: anything typed at all).
function tdDirty() {
  if (!editing) return false;
  return editing.id ? Object.keys(editBody()).length > 0
    : !!($("#tdContent").value.trim() || stepsNow().length || $("#tdNotes").value.trim());
}
// Esc, or the phone's back gesture: never throws away what was typed without asking.
// Esc is handled as a key, so it never becomes a close request: Chrome lets a page refuse only one close
// request in a row without a tap in between, so a second "Cancel" closed it anyway (W3-02). The back
// gesture is still a close request; if the browser closes it after "Cancel", it opens again as it was.
const tdAsk = () => !tdDirty() || confirm("Close without saving? Your changes will be lost.");
let tdKeep = null;  // what had focus, when it must open again
$("#taskDialog").addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  e.preventDefault();
  if (!tdBusy() && tdAsk()) $("#taskDialog").close();
});
$("#taskDialog").addEventListener("cancel", (e) => {
  // Saving (it closes by itself in a moment), or "Cancel": it stays.
  if (tdBusy() || !tdAsk()) { e.preventDefault(); if (!e.cancelable) tdKeep = document.activeElement; }
});
$("#taskDialog").addEventListener("close", () => {
  if (!tdKeep) return;
  const el = tdKeep; tdKeep = null;
  $("#taskDialog").showModal(); if ($("#taskDialog").contains(el)) el.focus();
});
// While there are unsaved changes, the print button says it saves them first.
const FOCUS_LABEL = $("#tdFocus").textContent;
function focusLabel() {
  const dirty = !!editing && (!editing.id || Object.keys(editBody()).length > 0);
  $("#tdFocus").textContent = dirty ? "Save & " + FOCUS_LABEL.charAt(0).toLowerCase() + FOCUS_LABEL.slice(1) : FOCUS_LABEL;
}
$("#taskForm").addEventListener("input", focusLabel);
$("#taskForm").addEventListener("change", focusLabel);
$("#taskForm").onsubmit = async (e) => {
  if (e.submitter?.value !== "save" || !editing) return;
  e.preventDefault();
  const autoDay = !$("#tdDate").value && !editing.due && ($("#tdTime").value || $("#tdRepeat").value);
  if (tdBusy()) return;  // a double tap mustn't add it twice
  await tdHold(async () => {
    try {
      const body = await saveEditing();
      $("#taskDialog").close();
      if (body.created) {
        await loadList();
        const hidden = filtering() && !matches(body.created);
        toast(hidden ? "Added. Your search hides it: tap Show all to see it." : autoDay ? "Added for today" : "Added");
        flashRows([body.created.id]);
        return;
      }
      toast(!Object.keys(body).length ? "Nothing changed" : autoDay ? "Saved for today" : "Saved");
      loadList();
    }
    catch (err) { listError(err, editing?.id); }
  });
};
$("#tdDelete").onclick = async () => {
  if (!editing || !confirm(`Delete "${editing.content}"? This can't be undone.`)) return;
  try { await api(`/list/${encodeURIComponent(editing.id)}?confirm=true`, { method: "DELETE" }); $("#taskDialog").close(); toast("Deleted"); loadList(); }
  catch (err) { listError(err, editing?.id); }
};
// Print just this task. Unsaved changes are saved first, so what prints is what you see.
$("#tdFocus").onclick = async () => {
  if (!editing || tdBusy()) return;
  if (!$("#tdContent").value.trim()) return $("#taskForm").reportValidity();
  try { await tdHold(saveEditing); } catch (err) { return listError(err, editing?.id); }
  const id = editing.id;  // a new task has one now
  $("#taskDialog").close();
  loadList();
  printFrom("/print/focus", { task_id: id }, $("#tdFocus"));
};

// tags (Settings)
async function loadTags() {
  let r;
  try { r = await api("/tags"); } catch (e) { $("#tagList").textContent = e.message; return; }
  listData.tags = r.tags;
  const opts = (sel) => r.colors.map((c) => `<option value="${esc(c)}" ${c === sel ? "selected" : ""}>${esc(c)}</option>`).join("");
  $("#newTagColor").innerHTML = opts("yellow");
  $("#tagList").innerHTML = r.tags.map((t) => `<div class="tagrow" data-name="${esc(t.name)}">${tagChip(t.name)}
      <select aria-label="Color for ${esc(t.name)}">${opts(t.color)}</select>
      <button class="plain small" data-del style="margin-left:auto" aria-label="Remove tag ${esc(t.name)}">Remove</button></div>`).join("") || `<p class="muted">No tags.</p>`;
}
$("#tagList").addEventListener("change", async (e) => {
  const row = e.target.closest(".tagrow"), name = row.dataset.name;
  try { await api(`/tags/${encodeURIComponent(name)}`, { method: "PUT", json: { color: e.target.value } }); toast("Saved"); }
  catch (err) { toast(errText(err)); }
  await loadTags();  // drawn again: focus goes back to the same tag's color, so arrow keys carry on
  $(`#tagList [data-name="${CSS.escape(name)}"] select`)?.focus();
});
$("#tagList").addEventListener("click", async (e) => {
  if (!e.target.matches("[data-del]")) return;
  const name = e.target.closest(".tagrow").dataset.name;
  const uses = (listData.tags.find((t) => t.name === name) || {}).uses || 0;
  const on = uses === 1 ? "1 task has it" : `${uses} tasks have it`;
  if (!confirm(uses ? `Remove the tag "${name}"? ${on}: it comes off ${uses === 1 ? "that task" : "them"} too, and they keep their other tags.`
                    : `Remove the tag "${name}"?`)) return;
  try { await api(`/tags/${encodeURIComponent(name)}?strip=true`, { method: "DELETE" }); loadTags(); loadList(); } catch (err) { toast(err.message); }
});
$("#tagForm").onsubmit = (e) => { e.preventDefault(); hold($("#addTag"), addTag); };  // a form, so Enter adds it too
async function addTag() {
  const typed = $("#newTag").value.trim();
  if (!typed) { toast("Type a tag name first"); $("#newTag").focus(); return; }
  try {
    const r = await api("/tags", { method: "POST", json: { name: typed, color: $("#newTagColor").value } });
    $("#newTag").value = "";
    // Tag names are short, lowercase letters, numbers, - and _: say so when what was typed changed.
    toast(r.name === typed.replace(/^[#@]+/, "").toLowerCase() ? `Added #${r.name}` : `Added "${typed}" as #${r.name}`, 3500);
    loadTags();
  } catch (err) { toast(errText(err), { error: true }); }
}
// The Edit task tag chips say whether each is on (aria-pressed), however they're drawn or toggled.
new MutationObserver(() => {
  $$("#tdTags [data-tag]").forEach((b) => {
    const on = String(b.classList.contains("on"));
    if (b.getAttribute("aria-pressed") !== on) b.setAttribute("aria-pressed", on);
  });
}).observe($("#tdTags"), { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });

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
// The count tells two similar names apart; a long name is cut so a list never pushes the page sideways.
const homeName = (h) => `${esc(h.name.length > 32 ? h.name.slice(0, 31) + "…" : h.name)} (${h.members})`;

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
  $("#snGo").onclick = () => hold($("#snGo"), async () => {  // one tap, one person
    try {
      const r = await api("/super/users", { method: "POST", json: { username: $("#snName").value, email: $("#snEmail").value || null,
        role: $("#snRole").value, household_id: $("#snHome").value, beta: $("#snBeta").checked } });
      renderTemp($("#snOut"), r.username, r.temp_password);
      $("#snName").value = $("#snEmail").value = ""; $("#snBeta").checked = false;  // ready for the next person
      loadSuper();
    } catch (e) { toast(errText(e), { error: true }); }
  });
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
    <dt>Tags</dt><dd>${u.tag_count ? plural(u.tag_count, "tag") : "none"}</dd></div>
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
      · ${i.status === "used" ? (i.used_by === "(deleted account)" ? "used by an account that's since been deleted" : "used by " + esc(i.used_by)) : i.status === "expired" ? "expired" : "open until " + when(i.expires)}
      ${i.status === "open" ? `<button class="linkish" data-copylink aria-label="Copy link for ${esc(i.code)}">copy link</button>
        · <button class="linkish" data-revoke aria-label="Cancel ${esc(i.code)}">cancel</button>` : ""}</div>`;
  // Open codes first; used and expired ones fold away and can be cleared.
  const open = list.filter((i) => i.status === "open"), done = list.filter((i) => i.status !== "open");
  $("#ivList").innerHTML = (open.map(row).join("") || (done.length ? "No open invites." : "None yet.")) +
    (done.length ? `<details style="margin-top:6px"><summary>Older invites (${done.length})</summary>${done.map(row).join("")}
      <div class="row"><button class="linkish" id="ivClear">Clear used and expired</button></div></details>` : "");
}
$("#ivOwn").onchange = () => { $("#ivHome").disabled = $("#ivOwn").checked; };
$("#ivGo").onclick = () => hold($("#ivGo"), makeInvite);  // one tap, one code
async function makeInvite() {
  try {
    const r = await api("/admin/invites", { method: "POST", json: { note: $("#ivNote").value, beta: $("#ivBeta").checked,
      own_household: $("#ivOwn").checked, days: Number($("#ivDays").value),
      household_id: me.role === "superadmin" ? $("#ivHome").value : null } });
    const link = location.origin + r.link;
    $("#ivOut").innerHTML = `<p tabindex="-1" data-temphead>Send them this code or link. It works once.</p><div class="secret">${esc(r.code)}</div>
      <div class="muted" style="word-break:break-all">${esc(link)}</div>
      ${["localhost", "127.0.0.1", "::1"].includes(location.hostname) ? `<p class="err">That link only works on this computer, because you opened Next Box as "${esc(location.hostname)}". Send the code instead, or open Next Box at the phone address that <code>nextbox doctor</code> shows and make the invite there.</p>` : ""}
      <div class="row"><button class="plain small" id="ivCopy">Copy link</button></div>`;
    $("#ivCopy").onclick = () => copyText(link);
    $("#ivOut [data-temphead]").focus();  // a screen reader says the new code is there (W3-06); Tab goes on to Copy link
    $("#ivNote").value = "";
    loadInvites();
  } catch (e) { toast(errText(e), { error: true }); }
}
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
      <button class="plain small" data-remove aria-label="Remove calendar ${esc(c.name)}">Remove</button>
      <div class="calstatus" style="flex-basis:100%"></div></div>`).join("") || `<p class="muted">No calendars yet. Add one to see today's events here and on your daily label.</p>`;
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
  try { r = await api("/calendars/today"); } catch { $("#todayCard").hidden = true; todayCal = { failed: true, events: [], problems: [] }; return calChanged(); }
  todayCal = r;
  $("#todayCard").hidden = !r.events.length && !r.problems.length;
  $("#todayEvents").innerHTML = r.events.map((e) => `<div class="mini"><b style="display:inline-block;min-width:70px">${e.all_day ? "all day" : esc(fmtClock(e.start))}</b>${esc(e.title)}${e.location ? ` <span class="muted">· ${esc(e.location)}</span>` : ""}</div>`).join("")
    + r.problems.map((p) => `<div class="mini err">${esc(p)}</div>`).join("");
  calChanged();
}


// ---------------------------------------------------------------- My list: List | Schedule
// Schedule puts today in one column by time: calendar events (read-only), timed tasks and the routine's
// "Plan your day" and "Snap your pages", with a Now line and "Next: … in 20 min". Tasks without a time follow
// under "Any time today" in the order today's label prints them. Anyone can pick it; "Same words every
// time" (routine_mode) makes it the view My list opens on. The pick is kept on this device.
let todayCal = null;   // GET /calendars/today as last loaded, { failed: true } if it couldn't load, null before
let viewPick = "";     // this visit's List/Schedule pick (also kept on this device)
let schedItems = [], schedTimer = null;
const myListViewKey = () => (me ? `nb.listView:${me.username}` : "");
function myListView() {
  let v = viewPick;
  if (!v) { try { v = localStorage.getItem(myListViewKey()) || ""; } catch {} }
  return v === "list" || v === "schedule" ? v : me?.prefs?.routine_mode ? "schedule" : "list";
}
function forgetListView() {
  viewPick = "";
  try { localStorage.removeItem(myListViewKey()); } catch {}
}
// The toggle shows the view, and Today's calendar card steps aside while Schedule shows the events itself.
function markListView() {
  const v = myListView();
  $$("#listView [data-view]").forEach((b) => { b.classList.toggle("on", b.dataset.view === v); b.setAttribute("aria-pressed", String(b.dataset.view === v)); });
  if (todayCal) $("#todayCard").hidden = v === "schedule" || (!todayCal.events.length && !todayCal.problems.length);
  return v;
}
function calChanged() { if (me && myListView() === "schedule") renderList(); else markListView(); }
$("#listView").onclick = (e) => {
  const b = e.target.closest("[data-view]");
  if (!b || b.getAttribute("aria-pressed") === "true") return;
  viewPick = b.dataset.view;
  try { localStorage.setItem(myListViewKey(), viewPick); } catch {}
  renderList();
};

const schedMinutes = (hhmm) => +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5);
const schedClock = (min) => { const m = Math.min(Math.max(min, 0), 24 * 60 - 1); return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
const schedNowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const schedIn = (min) => (min < 60 ? `${min} min` : `${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ""}`);

// Returns { html, shown: the open tasks it draws, total: today's open tasks }. Search my list and the
// tag chips work here too, on today's tasks; "Later" says how many more match in List.
function scheduleHtml() {
  const now = today(), p = me?.prefs || {};
  const day = (t) => (t.due ? t.due.date.slice(0, 10) : "");
  const timed = (t) => day(t) === now && t.due.date.length > 10;
  const shown = filtering() ? matches : () => true;
  const all = [...listData.tasks.filter((t) => !t.done), ...(listData.linked || [])];
  const total = all.filter((t) => day(t) && day(t) <= now).length;
  const open = all.filter(shown);
  const events = todayCal?.events || [];
  const items = [];
  for (const t of open.filter(timed)) {
    const start = schedMinutes(t.due.date.slice(11, 16));
    items.push({ kind: "task", start, end: t.minutes ? start + t.minutes : null, name: t.content, t });
  }
  for (const e of events.filter((x) => !x.all_day && x.start)) items.push({ kind: "event", start: schedMinutes(e.start), end: e.end ? schedMinutes(e.end) : null, name: e.title, e });
  items.push({ kind: "plan", start: schedMinutes(p.routine_morning || "08:00"), end: null });
  items.push({ kind: "look", start: schedMinutes(p.routine_evening || "18:00"), end: null });
  const order = { plan: 0, look: 0, event: 1, task: 2 };
  items.sort((a, b) => a.start - b.start || order[a.kind] - order[b.kind]);
  schedItems = items;
  // Any time today: as today's label orders them (urgent, today, then waiting, newest first). My list
  // comes in that order already; the linked app's tasks are slotted in by the same rule.
  const late = (t) => Math.round((new Date(now) - new Date(day(t))) / 86400000);
  const key = (t) => [t.priority >= 3 ? 0 : day(t) === now ? 1 : 2, -(t.priority || 1), late(t)];
  const anytime = open.filter((t) => day(t) && day(t) <= now && !timed(t)).map((t, i) => [t, i])
    .sort(([a, i], [b, j]) => { const x = key(a), y = key(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || i - j; })
    .map(([t]) => t);
  const later = listData.tasks.filter((t) => !t.done && (!t.due || day(t) > now) && shown(t)).length;
  const drawn = [...open.filter(timed), ...anytime];
  const allDay = events.filter((e) => e.all_day);
  let html = `<p class="schednext" id="schedNext"></p>`;
  if (filtering() && !drawn.length && total) html += noMatchHtml("today");
  if (!todayCal) html += `<p class="muted">Loading today's calendar…</p>`;
  else if (todayCal.failed) html += `<p class="err">Couldn't load your calendar, so its events aren't here. <button class="linkish" data-cal-retry>Try again</button></p>`;
  html += (todayCal?.problems || []).map((x) => `<p class="err">${esc(x)}</p>`).join("");
  if (allDay.length) html += `<div class="group" id="schedAllDay">All day</div><ul class="sched" aria-labelledby="schedAllDay">${allDay.map((e) => schedEventRow(e)).join("")}</ul>`;
  html += `<div class="group" id="schedHead">Today by time</div><ol class="sched" id="schedList" aria-labelledby="schedHead">${items.map(schedRow).join("")}</ol>`;
  if (!filtering() && !items.some((x) => x.kind === "task" || x.kind === "event")) {
    html += `<p class="muted">Nothing at a set time today. Give a task a time in Brain dump, like “call Sam 3pm”.</p>`;
  }
  html += `<div class="group">Any time today (${anytime.length})</div>`
    + (anytime.length ? anytime.map(taskRow).join("") : `<p class="muted">${filtering() ? "Nothing here matches." : "Nothing else due today."}</p>`);
  if (showingDone) {
    const done = listData.tasks.filter((t) => t.done && shown(t));
    html += `<div class="group">Done (${done.length})</div>`
      + (done.length ? done.map(taskRow).join("") : `<p class="muted">${filtering() ? "Nothing here matches." : "Nothing finished yet. It'll show here when you tick something."}</p>`);
  }
  if (later) html += `<p class="muted">Later: ${later === 1 ? "1 more task" : `${later} more tasks`} ${filtering() ? (later === 1 ? "matches" : "match") : "on your list"}, coming up or someday. <button class="linkish" data-view-go="list">Show them in List</button></p>`;
  return { html, shown: drawn, total };
}
function schedEventRow(e, x = null) {
  const meta = [e.location, e.calendar].filter(Boolean).map(esc).join(" · ");
  const time = x ? `${esc(fmtClock(schedClock(x.start)))}${x.end ? `<span>to ${esc(fmtClock(schedClock(x.end)))}</span>` : ""}` : "all day";
  return `<li class="srow event"${x ? ` data-start="${x.start}" data-end="${x.end ?? ""}"` : ""}><div class="stime">${time}</div>
    <div class="sbody"><div class="text"><span aria-hidden="true">📅 </span><span class="sr-only">Calendar: </span>${esc(e.title)}<span class="nowtag" hidden>now</span></div>
    ${meta ? `<div class="meta">${meta}</div>` : ""}</div></li>`;
}
function schedRow(x) {
  const time = esc(fmtClock(schedClock(x.start)));
  if (x.kind === "event") return schedEventRow(x.e, x);
  if (x.kind === "task") {
    // Its time is in the column, so the row doesn't say "today 9:00a" again (the buttons act on the task by its id).
    return `<li class="srow" data-start="${x.start}" data-end="${x.end ?? ""}"><div class="stime">${time}</div>${taskRow({ ...x.t, due: null })}</li>`;
  }
  // The words follow their setup answers: no printer yet (the label is optional), no journal (nothing to write in).
  const p = me?.prefs || {}, paper = p.print_with !== "none", journal = p.journal_mode !== false;
  // Print or Preview follows the Preview only box, in the line and on its button, as the print itself does.
  const label = `${$("#dry").checked ? "Preview" : "Print"} today's label`;
  const [name, what, action] = x.kind === "plan"
    ? ["Plan your day", paper ? `${label} and pick your one thing.` : `Pick your one thing. ${label} if you like.`,
       `<button class="linkish" data-sched-print>${label}</button>`]
    : journal || paper
      ? ["Snap your pages", journal ? (paper ? "Tick your label, write a few notes, snap both pages." : "Write a few notes, then snap your journal page.")
          : "Tick your label with a pen, then snap it.", `<button class="linkish" data-go-tab="scan">Open Snap</button>`]
      : ["Wrap up your day", "Tick off what you did today on My list.",
         aiOn() ? `<button class="linkish" data-sched-look>Look back on my week</button>` : ""];
  return `<li class="srow routine" data-start="${x.start}" data-end=""><div class="stime">${time}</div>
    <div class="sbody"><div class="text">${name}</div><div class="meta">${what} ${action}</div></div></li>`;
}
function renderSchedule(spot) {
  const s = scheduleHtml();
  $("#listBody").innerHTML = s.html;
  $("#listBody").classList.remove("muted");
  applyFilterUi(s.shown, s.total);
  updatePicks();
  renderNow();
  restoreFocus(spot);
  tickSchedule();
  if (!schedTimer) schedTimer = setInterval(tickSchedule, 30000);
}
// Every half minute while Schedule shows: the Now line moves, finished events go grey, "Next" counts down.
// Only these change, so whatever you're reading or have focused stays where it is.
function tickSchedule() {
  const list = $("#schedList");
  if (!list || !me || myListView() !== "schedule" || !$("#mylist").classList.contains("on") || document.visibilityState !== "visible") return;
  if (lastLoad.day && today() !== lastLoad.day) { loadList(); loadToday(); return; }  // past midnight: a new day
  const n = schedNowMin();
  let line = list.querySelector(".nowline");
  if (!line) { line = document.createElement("li"); line.className = "nowline"; }
  line.textContent = `Now ${fmtClock(schedClock(n))}`;
  const rows = [...list.querySelectorAll(":scope > .srow")];
  const ahead = rows.find((r) => +r.dataset.start > n);
  if (ahead) { if (line.nextElementSibling !== ahead) list.insertBefore(line, ahead); }
  else if (list.lastElementChild !== line) list.appendChild(line);
  for (const r of rows) {
    const s = +r.dataset.start, e = r.dataset.end === "" ? null : +r.dataset.end;
    if (!r.querySelector(".task")) r.classList.toggle("past", e != null ? e <= n : s < n);  // a task stays as it is until it's ticked
    const tag = r.querySelector(".nowtag");
    if (tag) tag.hidden = !(e != null && s <= n && n < e);
  }
  const real = schedItems.filter((x) => x.kind === "task" || x.kind === "event");
  const cur = real.find((x) => x.end != null && x.start <= n && n < x.end);
  const next = real.find((x) => x.start > n);
  const short = (s) => (s.length > 60 ? s.slice(0, 57) + "…" : s);
  $("#schedNext").innerHTML = [
    cur ? `<b>Now:</b> ${esc(short(cur.name))}, until ${esc(fmtClock(schedClock(cur.end)))}` : "",
    next ? `<b>Next:</b> ${esc(short(next.name))} in ${schedIn(next.start - n)} (${esc(fmtClock(schedClock(next.start)))})` : "",
  ].filter(Boolean).join("<br>") || (real.length ? "Nothing else at a set time today." : "");
}
document.addEventListener("visibilitychange", () => tickSchedule());
$("#listBody").addEventListener("click", (e) => {
  if (e.target.closest("[data-view-go]")) { viewPick = "list"; try { localStorage.setItem(myListViewKey(), "list"); } catch {} renderList(); $("#listView [data-view=list]").focus(); }
  else if (e.target.closest("[data-sched-print]")) $("#printTodayList").click();
  else if (e.target.closest("[data-sched-look]")) goto("journal", "#lookCard");
  else if (e.target.closest("[data-cal-retry]")) { todayCal = null; renderList(); loadToday(); }
});
$("#dry").addEventListener("change", () => { if (me && myListView() === "schedule") renderList(); });  // "Preview" or "Print today's label"


// ---------------------------------------------------------------- setup questions
// New accounts answer these once, after the data notice (Settings → Help runs them again). One question
// per screen with big buttons; any question, or all of them, can be skipped. The answers are saved
// together at the end as ordinary settings (POST /setup/quiz), and the last screen says what changed
// and where to change it later.
const QUIZ = [
  { key: "print", q: "How will you print?", hint: "Pick the closest. You can change it any time.",
    opts: [["label", "Label printer", () => (printsHere() ? "A Brother or other label printer" : "Save each label as an image for your label printer's app")],
           ["paper", "Regular printer", "Any home or office printer: you print previews on paper"],
           ["none", "No printer yet", "Use Next Box on screen and add a printer later"]],
    now: (p) => p.print_with || null },
  { key: "journal", q: "Do you keep a paper journal or planner?", hint: "Today's label goes on the left page and your notes on the right. Next Box keeps a photo of both.",
    opts: [["a5", "Yes, A5", "About 6 × 8 inches"], ["half_letter", "Yes, half letter", "5.5 × 8.5 inches"],
           ["letter", "Yes, letter", "8.5 × 11 inches"], ["a4", "Yes, A4", "21 × 30 cm"], ["none", "No journal", "Just the list, please"]],
    now: (p) => (p.journal_mode ? p.paper_size : "none") },
  { key: "rows", q: "How many tasks a day feels doable?", hint: "That many go on your daily label. The rest wait, listed small at the bottom.",
    opts: [[3, "3 tasks", "A short, calm list"], [5, "5 tasks", "A few more"], [8, "8 tasks", "A full day"]],
    now: (p) => p.max_rows },
  { key: "helps", multi: true, q: "What helps you most?", hint: "Pick any that fit.",
    opts: [["gentle", "Short lists and gentle words", "“Waiting 3 days”, never “overdue”. Often picked by people with ADHD."],
           ["reading", "Easy-to-read text", "A font made for reading, with more space. Often picked by people with dyslexia."],
           ["routine", "Same routine every day, no surprises", "The same words every time, and your day in time order. Often picked by autistic people."],
           ["basics", "Just the basics", "None of these extras"]] },
  { key: "times", q: "When do you plan your day, and when do you snap your pages?", hint: "Both show on My list's Schedule, so every day has the same shape.", times: true },
];
let quiz = { i: 0, answers: {}, rerun: false, saving: false };
let quizAsked = false;  // shown by itself once per visit at most
// manual: opened from Settings → Help or the guide. It starts from their settings as they are now (an
// account from before the questions existed has some on), and Esc or Skip changes nothing.
function openQuiz(manual = false) {
  quiz = { i: 0, answers: {}, rerun: manual || !!me?.prefs?.setup_quiz_done, saving: false };
  if (!$("#quizDialog").open) $("#quizDialog").showModal();
  drawQuiz();
}
const quizAnswered = () => Object.keys(quiz.answers).length > 0;
function drawQuiz() {
  const q = QUIZ[quiz.i], p = me?.prefs || {};
  $("#quizMsg").textContent = "";
  $("#quizStep").textContent = `Question ${quiz.i + 1} of ${QUIZ.length}${quiz.i === 0 && !quiz.rerun ? " · about a minute" : ""}`;
  $("#quizBar").style.width = `${Math.round(100 * quiz.i / QUIZ.length)}%`;
  $("#quizSkip").hidden = false;
  $("#quizSkip").textContent = quizAnswered() ? "Skip the rest" : "Skip setup";
  $("#quizQ").textContent = q.q;
  $("#quizHint").textContent = q.hint;
  const was = quiz.answers[q.key];
  if (q.times) {
    $("#quizBody").innerHTML = `<div class="quiztimes">
      <div class="field"><label for="quizPlan">Plan my day at</label><input type="time" id="quizPlan" value="${esc(quiz.answers.plan_at || quiz.draft?.plan_at || p.routine_morning || "08:00")}"></div>
      <div class="field"><label for="quizLook">Snap my pages at</label><input type="time" id="quizLook" value="${esc(quiz.answers.review_at || quiz.draft?.review_at || p.routine_evening || "18:00")}"></div></div>`;
  } else {
    const current = quiz.rerun && q.now ? q.now(p) : null;
    const on = (v) => (q.multi ? (was || (quiz.rerun ? quizHelpsNow(p) : [])).includes(v) : was === v);
    $("#quizBody").innerHTML = `<div class="quizopts" role="group" aria-labelledby="quizQ">${q.opts.map(([v, name, sub]) =>
      // Several to pick: toggle buttons. One to pick: plain buttons that answer and move on (going Back
      // shows the answer given, and a second run shows the setting as it is now).
      q.multi ? `<button type="button" class="quizopt" data-answer="${esc(v)}" aria-pressed="${on(v)}">
        <span class="tick" aria-hidden="true">${on(v) ? "✓" : ""}</span>
        <span><b>${esc(name)}</b><span class="muted">${esc(val(sub))}</span></span></button>`
      : `<button type="button" class="quizopt${on(v) ? " chosen" : ""}" data-answer="${esc(v)}">
        <span><b>${esc(name)}</b><span class="muted">${esc(val(sub))}</span></span>
        ${on(v) ? `<span class="quiznow">Your answer</span>` : current === v && was === undefined ? `<span class="quiznow">Your setting now</span>` : ""}</button>`).join("")}</div>`;
  }
  $(".quiznav").hidden = false;
  $("#quizBack").hidden = quiz.i === 0;
  $("#quizPass").hidden = false;
  $("#quizNext").hidden = !q.multi && !q.times;
  $("#quizNext").textContent = quiz.i === QUIZ.length - 1 ? "Finish" : "Next";
  $("#quizQ").focus();
}
// On a second run, what's already on (so "Next" alone keeps things as they are).
function quizHelpsNow(p) {
  const on = [p.gentle_words && "gentle", p.reading_mode && "reading", p.routine_mode && "routine"].filter(Boolean);
  return on.length ? on : ["basics"];
}
function quizStep(delta) {
  quiz.i += delta;
  if (quiz.i >= QUIZ.length) return saveQuiz();
  drawQuiz();
}
$("#quizBody").addEventListener("click", (e) => {
  const b = e.target.closest("[data-answer]");
  if (!b) return;
  const q = QUIZ[quiz.i];
  if (!q.multi) {
    quiz.answers[q.key] = q.key === "rows" ? +b.dataset.answer : b.dataset.answer;
    return quizStep(1);
  }
  // Several can be picked; "Just the basics" goes on its own.
  const v = b.dataset.answer, opts = $$("#quizBody [data-answer]");
  const press = (el, yes) => { el.setAttribute("aria-pressed", String(yes)); el.querySelector(".tick").textContent = yes ? "✓" : ""; };
  const yes = b.getAttribute("aria-pressed") !== "true";
  press(b, yes);
  if (yes) for (const o of opts) if (o !== b && (v === "basics" || o.dataset.answer === "basics")) press(o, false);
});
$("#quizNext").onclick = () => {
  const q = QUIZ[quiz.i];
  if (q.times) {
    const plan = $("#quizPlan").value, look = $("#quizLook").value;
    if (!plan || !look) { $("#quizMsg").textContent = "Pick both times, or skip this question."; return (plan ? $("#quizLook") : $("#quizPlan")).focus(); }
    Object.assign(quiz.answers, { plan_at: plan, review_at: look });
  } else if (q.multi) {
    const picked = $$("#quizBody [data-answer][aria-pressed=true]").map((b) => b.dataset.answer);
    if (picked.length) quiz.answers.helps = picked; else delete quiz.answers.helps;  // none picked: skipped
  }
  quizStep(1);
};
$("#quizPass").onclick = () => {  // skip this one: whatever was picked here before is forgotten
  const q = QUIZ[quiz.i];
  for (const k of q.times ? ["plan_at", "review_at"] : [q.key]) delete quiz.answers[k];
  quizStep(1);
};
$("#quizBack").onclick = () => {  // times typed but not yet saved with Next come back when you return
  if (QUIZ[quiz.i]?.times) quiz.draft = { plan_at: $("#quizPlan").value, review_at: $("#quizLook").value };
  quizStep(-1);
};
$("#quizSkip").onclick = () => skipQuiz();
$("#quizDialog").addEventListener("cancel", (e) => {  // Esc does what the Skip button says
  if (quiz.i >= QUIZ.length && !quiz.saving) return;   // the last screen: Esc just closes it
  e.preventDefault();
  if (!quiz.saving) skipQuiz();
});
async function skipQuiz() {
  if (quizAnswered()) { quiz.i = QUIZ.length; return saveQuiz(); }  // "Skip the rest": keep what was answered
  $("#quizDialog").close();
  if (quiz.rerun) return toast("Nothing changed.");
  try {
    const r = await api("/setup/quiz", { method: "POST", json: { skip: true } });
    me.prefs = r.prefs;
    renderGuide();
    toast("Skipped. These questions are in Settings → Help whenever you want them.");
  } catch (err) { toast(errText(err)); }
}
async function saveQuiz() {
  quiz.saving = true;
  $("#quizStep").textContent = "All done";
  $("#quizBar").style.width = "100%";
  $("#quizQ").textContent = "Saving your answers…";
  $("#quizHint").textContent = "";
  $("#quizBody").innerHTML = "";
  $(".quiznav").hidden = $("#quizSkip").hidden = true;  // the last screen has its own buttons
  $("#quizDialog").setAttribute("aria-busy", "true");
  let r;
  try { r = await api("/setup/quiz", { method: "POST", json: { answers: quiz.answers } }); }
  catch (err) {
    quiz.saving = false;
    $("#quizDialog").removeAttribute("aria-busy");
    $("#quizQ").textContent = "Couldn't save your answers";
    $("#quizHint").textContent = "Nothing was changed yet.";
    $("#quizMsg").textContent = errText(err);
    $("#quizBody").innerHTML = `<div class="row"><button type="button" class="go" data-quiz="retry">Try again</button>
      <button type="button" class="plain" data-quiz="back">Change my answers</button><button type="button" class="plain" data-quiz="close">Not now</button></div>`;
    return $("#quizQ").focus();
  }
  quiz.saving = false;
  $("#quizDialog").removeAttribute("aria-busy");
  me.prefs = r.prefs;
  // Everything that follows the settings shows them now: the look, the Preview box, My list, the guide.
  // A box in Settings still saying why an old value didn't save shows the new one instead.
  Object.keys(r.changed).forEach((k) => { const el = $(`[data-pref=${k}]`); if (el) prefErr(el, ""); });
  loadPrefs(me.prefs); applyLook(); refreshStatus();
  if ("routine_mode" in r.changed) forgetListView();
  renderList(); loadGuide();
  const lines = quizSummary(r.changed);
  $("#quizQ").textContent = lines.length ? "You're all set" : "Nothing changed";
  $("#quizHint").textContent = lines.length ? "Here's what changed:" : "Your settings are as they were.";
  $("#quizBody").innerHTML = (lines.length ? `<ul class="quizsum">${lines.map(([text, where]) =>
      `<li>${esc(text)}<span class="muted">Change it in ${esc(where)}.</span></li>`).join("")}</ul>` : "")
    + `<p class="muted">Run these questions again any time from Settings → Help.</p>
      <div class="row quizfoot"><button type="button" class="go" data-quiz="close">Start using Next Box</button>
      <button type="button" class="plain" data-quiz="settings">Open Settings</button></div>`;
  $("#quizQ").focus();
}
const PAPER_NAMES = { a5: "A5", half_letter: "half letter", letter: "letter", a4: "A4" };
function quizSummary(c) {
  const out = [];
  if ("print_with" in c) {
    // Printing is the default, and a real print always asks "Send this to the printer?" first.
    const none = c.print_with === "label" && printsHere() && printNow().printers === 0;  // a label printer, none added yet
    out.push([none ? `Label printer: ${NO_PRINTER.toLowerCase()} ${noPrinterNext()}`
      : { label: printsHere() ? "Printing on your label printer. Next Box asks “Send this to the printer?” before each print." : PRINT_AWAY,
          paper: "Printing on paper: under each ticket, tap Print on paper.",
          none: "No printer yet: tickets show on screen, and Next Box asks before anything goes to a printer." }[c.print_with],
    none ? "Settings → Printers & label colors" : "Settings → Printing"]);
  }
  if ("journal_mode" in c || "paper_size" in c) {  // journal mode starts on, so often only the size changed
    const journal = "journal_mode" in c ? c.journal_mode : me?.prefs?.journal_mode !== false;
    const size = c.paper_size || me?.prefs?.paper_size;
    out.push(journal
      ? [`Journal, ${PAPER_NAMES[size] || "letter"} size: your daily label gets a “Today's win” line, and every page you snap is kept in Journal.`, "Settings → Focus and Printing"]
      : ["No journal: the daily label has no journal lines, and a snap is kept in Journal only when it has handwriting (notes or a win).", "Settings → Focus"]);
  }
  if ("max_rows" in c) out.push([`${c.max_rows} tasks on your daily label. The rest are listed small at the bottom.`, "Settings → Focus"]);
  if ("gentle_words" in c) out.push([c.gentle_words ? "Gentle words: “waiting 3d”, never “overdue”." : "Gentle words: off.", "Settings → Focus"]);
  if ("reading_mode" in c) out.push([c.reading_mode ? "Easy-to-read text: on, here and on your labels." : "Easy-to-read text: off.", "Settings → General"]);
  if ("routine_mode" in c) out.push([c.routine_mode ? "Same words every time: on. My list opens on Schedule." : "Same words every time: off.", "Settings → Focus"]);
  if ("routine_morning" in c || "routine_evening" in c) {
    out.push([`Plan your day at ${fmtClock(me.prefs.routine_morning)} and snap your pages at ${fmtClock(me.prefs.routine_evening)}.`, "Settings → Focus"]);
  }
  return out;
}
$("#quizBody").addEventListener("click", (e) => {
  const act = e.target.closest("[data-quiz]")?.dataset.quiz;
  if (act === "retry") saveQuiz();
  else if (act === "back") { quiz.i = QUIZ.length - 1; drawQuiz(); }
  else if (act === "close") $("#quizDialog").close();
  else if (act === "settings") { $("#quizDialog").close(); goto("settings"); }
});
$("#quizAgain").onclick = () => openQuiz(true);
// Opened by itself (a new account), it had nothing to give focus back to: Just one thing, or the screen's heading.
$("#quizDialog").addEventListener("close", () => setTimeout(() => { if (document.activeElement === document.body || !document.activeElement) focusMain(); }, 0));


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
  if (!names.length) return "Todoist, Notion, Asana, monday.com or a CalDAV app";
  return names.length === 1 ? names[0] : names.slice(0, -1).join(", ") + " or " + names[names.length - 1];
}
function goto(tab, selector) {
  $(`#topnav button[data-tab=${tab}]`)?.click();
  setTimeout(() => { const el = selector && $(selector); if (el) { el.scrollIntoView({ behavior: smooth(), block: "start" }); el.querySelector?.("textarea,input,select")?.focus?.(); } }, 150);
}
let guide = null;
const STEPS = {
  tasks: { title: "Empty your head", text: "Type everything on your mind into Brain dump, one thing per line. Don't sort it.",
           go: ["Go to Brain dump", () => { $("#dumpText").scrollIntoView({ behavior: smooth(), block: "center" }); $("#dumpText").focus(); }] },
  home: { title: () => (isIOS() || isAndroid() ? "Put Next Box on your home screen" : "Install Next Box as an app"),
          text: () => isIOS() ? "In Safari tap Share (the square with the arrow) → Add to Home Screen. Then open Next Box from that icon."
                    : isAndroid() ? "In Chrome tap ⋮ → Add to Home screen (or Install app). Then open Next Box from that icon."
                    : "In Chrome or Edge, click the install icon at the right of the address bar. No icon? A bookmark works too.",
          go: () => (installPrompt ? ["Install", install] : null),
          mark: "I did it" },
  app: { title: "Link your to-do app (optional)", text: () => `Already use ${appNames()}? Its tasks show up here too.`,
         go: ["Link an app", () => goto("settings", "#appCard")], skip: true },
  calendar: { title: "Add your calendar (optional)", text: "Today's events show here and print at the top of your daily label. Google, Outlook and iCloud all work.",
              go: ["Add a calendar", () => goto("settings", "#calCard")], skip: true },
  printer: { title: () => !printsHere() ? "Printing your labels" : guide?.can_add_printer ? "Add your label printer" : "Your printer",
             text: () => !printsHere() ? PRINT_AWAY
               : guide?.printers === 0 ? `${NO_PRINTER} ${noPrinterNext()} Until then, every ticket has Print on paper, which works with any printer.`
               : guide?.can_add_printer ? "Admin → Printers for a label printer. No label printer? Every ticket has Print on paper, which works with any printer."
                                        : "No label printer? Every ticket has Print on paper, which works with any printer. A household admin can add a label printer later.",
             go: () => printsHere() && guide?.can_add_printer ? ["Add printer", () => goto("admin", "#printerList")] : null, skip: true },
  preview: { title: "Make your first stickers", text: "Tick a task or two and tap Stickers. With “Preview only” on, nothing prints: you just see how it looks.",
             go: ["Show me", () => $(".printbar").scrollIntoView({ behavior: smooth(), block: "center" })] },
  today: { title: "Print today's label", text: "Today's tasks on one label, with the next tiny step under each. Tap Print today. With “Preview only” on, you just see it.",
           go: ["Show me", () => { goto("print"); setTimeout(() => $("[data-act=today]").focus(), 200); }] },
  snap: { title: "Mark it and snap it", text: "Tick a row with a pen, then take a photo in Snap. Try it on a preview: print it on paper or hold the preview up to your camera.",
          go: ["Open Snap", () => goto("scan")], skip: true },
  focus: { title: "Make it fit your brain", text: "How many tasks per day, gentle wording, yesterday's wins, tiny steps: it's all in Focus & ADHD helpers.",
           go: () => (me?.prefs?.setup_quiz_done ? ["Open", () => goto("settings", "#focusCard")] : ["Answer 5 quick questions", () => openQuiz(true)]),
           mark: "Looks good" },
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
            ${d.skip ? `<button class="plain small" data-done>${s.key === "printer" && (!guide.can_add_printer || !printsHere()) ? "OK" : "Skip for now"}</button>` : ""}</div>` : ""}
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
// Each of these hides itself: keyboard focus goes on to what it opened, or back to the other one (W3-07).
$("#guideMini").onclick = () => {
  setGuideOpen(true);
  focusFirst($("#guideSteps .next .title"), $("#guideHead h2"));  // the next step (Tab goes on to its buttons), or the heading when all are done
};
$("#guideLess").onclick = () => { setGuideOpen(false); focusFirst($("#guideMini")); };
$("#guideHide").onclick = async () => {
  await api("/setup/guide/hide", { method: "POST" }).catch(() => {});
  $("#guideCard").hidden = true;
  focusMain();
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
$("#jOlder").onclick = async () => {
  const had = $$("#jDays .jday").length;
  await loadJournal(true);
  // Focus goes to the first day it brought in, where the button was, so Tab carries on through them; with none,
  // it stays on the button, or "That's everything." once that's all (W3-07).
  const a = document.activeElement;
  if (a === $("#jOlder") || !a?.offsetParent) focusFirst($$("#jDays .jday")[had]?.querySelector("h3"), $("#jOlder"), $("#jEnd"));
};

function winsHtml(w) {
  if (!w.count) return "";
  const named = new Map();
  w.items.forEach((t) => named.set(t, (named.get(t) || 0) + 1));
  const items = [...named].map(([t, n]) => `<li>${esc(t)}${n > 1 ? ` ×${n}` : ""}</li>`).join("");
  const linked = w.linked ? `<li class="muted">${items ? "+" : ""}${w.linked} ${items ? "more " : ""}in your linked app</li>` : "";
  const other = w.other && (items || linked) ? `<li class="muted">+${w.other} more</li>` : "";  // unnamed My list wins (older versions)
  return `<div data-wins><p class="muted" style="margin-bottom:0">Wins: ${w.count} ✓</p><ul class="winlist">${items}${linked}${other}</ul></div>`;
}

function pageHtml(p, d) {
  const made = new Date(p.created * 1000);
  const local = `${made.getFullYear()}-${String(made.getMonth() + 1).padStart(2, "0")}-${String(made.getDate()).padStart(2, "0")}`;
  const time = made.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const when = local === d.day ? time : `written ${made.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
  const id = esc(p.id);
  return `<div class="jpage" data-page="${id}" data-photo="${p.has_photo ? "1" : ""}">
      ${p.has_photo ? `<button class="jphoto" data-jphoto aria-label="Open the photo of your page, ${esc(dayName(d.day, d))} ${esc(when)}"><img loading="lazy" src="/journal/${id}/photo" alt=""></button>` : ""}
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
  const what = KIND_NAMES[t.kind] || "Ticket";  // the daily one: "Daily label", as everywhere
  const size = me?.prefs?.paper_size || "letter";
  return `<div class="jticket"><button type="button" data-jticket="${esc(t.id)}" aria-label="Open ${esc(what.toLowerCase())} #${esc(t.id)}">
      <img src="${png(t.id)}" alt="" loading="lazy"></button>
    <div><span class="muted">${esc(what)} #${esc(t.id)}${t.dry_run ? " (preview)" : ""}</span>
      <span class="jtacts"><a class="btnlink plain small" href="/printed/${encodeURIComponent(t.id)}/pdf?paper=${esc(size)}" target="_blank" rel="noopener"
         aria-label="Print ticket #${esc(t.id)} on paper">Print on paper</a>
      ${saveImageLink(t.id, "btnlink plain small")}</span></div></div>`;
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
      const next = page.nextElementSibling?.matches("[data-page]") ? page.nextElementSibling : null;
      const nextDay = day?.nextElementSibling, prevDay = day?.previousElementSibling;  // the oldest day has no next one (R2-04)
      page.remove();
      if (day && !day.querySelector(".jpage, [data-wins], .jwin, .jchanges")) day.remove();
      if (!$("#jDays .jday")) $("#jDays").innerHTML = NO_JOURNAL;
      focusFirst(next?.querySelector("textarea, button"), day?.isConnected ? day.querySelector("h3") : null,
                 nextDay?.querySelector?.("h3"), prevDay?.querySelector?.("h3"), $("#journal h2"));
      toast("Deleted");
    } catch (e) { toast(errText(e)); }
  });
}
$("#jDays").addEventListener("click", (e) => {  // a photo opens here, so its address stays out of the history
  const b = e.target.closest("[data-jphoto]");
  if (!b) return;
  $("#photoBig").src = b.querySelector("img").src;
  $("#photoBig").alt = b.getAttribute("aria-label").replace(/^Open the photo/, "Photo");  // which page this is
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
          <button class="plain small" data-slug-break data-ai${aiOn() && me?.ai_ready ? "" : " hidden"}>Break it down</button>
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
  const next = el.nextElementSibling || el.previousElementSibling;
  el.remove();
  if (!$("#slugList .slug")) $("#slugCard").hidden = true;
  focusFirst(next?.querySelector("button"), $("#slugCard:not([hidden]) h2"), $("#journal h2"));
  toast(msg);
}

// "Make it smaller": the task's tiny steps, right here.
const STEPS_HINT = $("#tdStepNew").placeholder;
async function makeSmaller(id) {
  if (!listData.tasks.some((t) => t.id === id)) await loadList();
  const t = listData.tasks.find((x) => x.id === id);
  if (!t) return toast(TASK_GONE);
  openTask(t);
  $("#tdStepNew").placeholder = "What's a 2-minute first step?";
  $("#tdStepNew").focus();
}
$("#taskDialog").addEventListener("close", () => {
  $("#tdStepNew").placeholder = STEPS_HINT;
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
$("#jCancel").onclick = () => { $("#jNew").hidden = true; $("#jWrite").focus(); };
$("#jSave").onclick = () => hold($("#jSave"), async () => {  // a double tap mustn't make two pages
  if (!$("#jText").value.trim()) return toast("Write something first");
  try {
    await api("/journal", { method: "POST", json: { notes: $("#jText").value, day: $("#jDay").value || null } });
    $("#jText").value = ""; $("#jNew").hidden = true; toast("Saved to your journal"); loadJournal();
    $("#jWrite").focus();  // Save is hidden now: carry on from Write a note
  } catch (e) { toast(errText(e), { error: true }); }
});

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
$("#jBackGo").onclick = async () => {
  const done = loadJournal(); window.scrollTo(0, 0); await done;
  if (!document.activeElement?.offsetParent) focusFirst($("#journal h2"));  // the button went away with its bar (W3-07)
};

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
    msg.querySelector("[data-go-tab]").focus();  // the Add button is off now: focus stays in the sheet
    loadList();
  } catch (e) {
    msg.className = "err";
    msg.textContent = e.status && typeof e.message === "string" && e.message !== "[object Object]" ? e.message
      : e.status ? "Some lines couldn't be added. Check them and try again." : OFFLINE;
    drawLines();
    ($("#linesAdd").disabled ? $("#linesClose") : $("#linesAdd")).focus();
  }
};
$("#linesClose").onclick = () => $("#linesDialog").close();
$("#linesDialog").addEventListener("click", (e) => { if (e.target.closest("[data-go-tab]")) $("#linesDialog").close(); });
$("#linesDialog").addEventListener("close", () => {
  const o = lines.opener;
  if (o?.isConnected && o.offsetParent) return o.focus();
  setTimeout(() => ($("#mylist").classList.contains("on") ? $("#listTitle").focus() : focusMain()), 0);  // "Open My list": the list, where they went
});

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
  if (e.target.closest("a[data-export-data]")) { e.preventDefault(); exportMyData(); }
});
// Download my data (.zip): asks first, so a paused download (after a password reset) or the hourly
// limit is a message here, not a page of raw JSON in place of the app. A second tap while it starts does
// nothing (it would use a second of the 5 an hour), and the file comes through a download link, so even a
// download that fails can never take the app's place.
async function exportMyData() {
  if (exportMyData.busy) return;
  exportMyData.busy = true;
  setTimeout(() => { exportMyData.busy = false; }, 8000);
  try { await api("/auth/export/check"); } catch (e) { exportMyData.busy = false; return toast(errText(e), { error: true }); }
  toast("Getting your .zip ready…");
  const a = Object.assign(document.createElement("a"), { href: "/auth/export?format=zip", download: "", hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
}
$("#jxDialog").addEventListener("change", jxSummary);
$("#jxGo").addEventListener("click", (e) => {
  if (!$("#jxGo").href) return e.preventDefault();
  $("#jxSummary").className = "muted";
  $("#jxSummary").textContent = "Your download is starting. A big journal can take a moment to put together.";
});
$("#jxClose").onclick = () => $("#jxDialog").close();
closeOnBackdrop($("#jxDialog"));


// ---------------------------------------------------------------- AI helpers (nextbox/coach.py)
// Only ever when the person taps. "Break it down" suggests tiny first steps to tick and edit before
// anything is saved. A linked app's task is never changed: its steps can go on a new My list task or
// be printed on a Just one thing card. "Look back on my week" writes a short note that's kept only if
// they save it to their journal. Settings → Focus → AI helpers hides both.
const aiOn = () => me?.prefs?.ai_helpers !== false;
function applyAi(on = aiOn()) {
  const ready = !!me?.ai_ready;
  // Without an AI key there's no Break it down button to dead-end on. Look back stays, to say what's missing.
  $$("[data-ai]").forEach((el) => { el.hidden = !on || (!ready && el.id !== "lookCard"); });
  $("#lookNoKey").textContent = noKeyText();
  $("#lookNoKey").hidden = ready; $("#lookAsk").hidden = !ready;
}  // loadPrefs calls it, so it follows the setting once it's saved (or put back if saving failed)

// Exactly what coach.breakdown_message sends (privacy.html says the same).
const BREAK_SENT = "This sends the task's name, how long it takes, its day, its steps and anything you type in “What makes it hard?” to Anthropic's AI. Nothing is kept unless you save the steps.";
// The first time on this device it says that before anything is sent, and asks only on "Suggest steps".
const breakSeenKey = () => (me ? `nb.breakSeen:${me.username}` : "");
function breakSeen() { try { return localStorage.getItem(breakSeenKey()) === "1"; } catch { return false; } }
function breakSeenNow() { try { localStorage.setItem(breakSeenKey(), "1"); } catch {} }
let brk = null;  // the sheet that's open: { id, content, from: edit|now|slug, opener, task, steps, replace, hint, ctl, changed }

// The Edit dialog's tiny steps: the one place this sheet reads or changes that dialog.
function editStepsNow() { return stepsNow().map((x) => x.text); }
function editStepsSet(texts, replace) {  // fills the Edit dialog's step list (FA's editor); saving stays with Save
  if (replace) setEditSteps(texts);
  else { addSteps(texts.join("\n")); drawSteps(); }
  focusLabel();  // "Save & print" notices the change
}

function openBreak(id, content, from, opener) {
  if (!id) return;
  brk?.ctl?.abort();
  brk = { id, content, from, opener, steps: [], replace: true, hint: "", changed: false };
  $("#breakTask").textContent = content || "";
  if (!$("#breakDialog").open) $("#breakDialog").showModal();
  if (!me?.ai_ready) return drawBreak("nokey");
  if (!breakSeen()) return drawBreak("intro");  // first time: what's sent, before it's sent
  askBreak();
}

async function askBreak(hint = "") {
  const b = brk;
  b.ctl?.abort();
  b.ctl = new AbortController();
  b.hint = hint;
  drawBreak("loading");
  try {
    const r = await api(`/list/${encodeURIComponent(b.id)}/breakdown`, { method: "POST", json: { hint }, signal: b.ctl.signal });
    if (brk !== b) return;
    b.task = r.task;
    $("#breakTask").textContent = r.task.content;
    if (r.support) return drawBreak("support", r.support);
    b.steps = r.steps.map((text) => ({ text, keep: true }));
    drawBreak("review");
  } catch (e) {
    if (brk !== b || e.name === "AbortError") return;
    if (e.message === TASK_GONE) b.changed = true;  // the list behind it is out of date
    drawBreak("error", errText(e));
  }
}

const keptSteps = () => (brk?.steps || []).filter((s) => s.keep && s.text.trim()).map((s) => s.text.trim());
function hadSteps() {
  const b = brk;
  return b.from === "edit" ? editStepsNow().length : (b.task?.steps || []).length;
}
function breakMsg(text = "", cls = "") { $("#breakMsg").textContent = text; $("#breakMsg").className = cls; }

function reviewHtml() {
  const b = brk, had = hadSteps(), them = had === 1 ? "it" : "them";
  const rows = b.steps.map((s, i) => `<li class="${s.keep ? "" : "off"}" data-i="${i}"><span class="bnum" aria-hidden="true">${i + 1}.</span>
      <input type="checkbox" ${s.keep ? "checked" : ""} aria-label="Keep step ${i + 1}">
      <textarea rows="1" maxlength="200" aria-label="Step ${i + 1}" placeholder="A tiny step">${esc(s.text)}</textarea></li>`).join("");
  const how = had && b.task.local ? `<fieldset class="bhow"><legend class="muted">It has ${plural(had, "step")} now.</legend>
      <label><input type="radio" name="bhow" value="replace"${b.replace ? " checked" : ""}> Use these instead</label>
      <label><input type="radio" name="bhow" value="add"${b.replace ? "" : " checked"}> Add these after ${them}</label></fieldset>` : "";
  const linked = b.task.local ? "" : `<p class="muted">These stay in Next Box. ${esc(b.task.app || "Your linked app")}'s task isn't changed.</p>`;
  return `<p class="muted">Tick the ones to keep, and change any words you like.</p>
    <ol class="bsteps">${rows}</ol>
    <button class="linkish" data-b="add">+ Add a step</button>
    ${how}${linked}
    <details class="bhint"${b.hint ? " open" : ""}><summary>Not quite right? Ask again</summary>
      <div class="field"><label for="bHint">What makes it hard? (optional)</label>
        <input type="text" id="bHint" maxlength="300" value="${esc(b.hint)}" placeholder="e.g. I don't know where the form is"></div>
      <div class="row"><button class="plain small" data-b="again">Ask again</button></div></details>`;
}
function reviewActs() {
  const b = brk, n = keptSteps().length, off = n ? "" : " disabled";
  if (b.from === "edit") return `<button class="go" data-b="use"${off}>Use ${n ? plural(n, "step") : "steps"}</button>`;
  if (b.task.local) return `<button class="go" data-b="save"${off}>Save ${n ? plural(n, "step") : "steps"}</button>`;
  return `<button class="go" data-b="copy"${off}>Add to My list</button><button class="plain" data-b="print"${off}>Print just this</button>`;
}
function drawBreakSteps() {  // each step's box is as tall as its words, so nothing is cut off
  $("#breakBody").innerHTML = reviewHtml();
  $$("#breakBody .bsteps textarea").forEach(fitText);
}
function countBreak() {
  const n = keptSteps().length, go = $("#breakActs .go");
  $$("#breakActs [data-b=use], #breakActs [data-b=save], #breakActs [data-b=copy], #breakActs [data-b=print]").forEach((x) => { x.disabled = !n; });
  if (go && ["use", "save"].includes(go.dataset.b)) go.textContent = `${go.dataset.b === "use" ? "Use" : "Save"} ${n ? plural(n, "step") : "steps"}`;
}

function drawBreak(state, info = "") {
  const b = brk, body = $("#breakBody"), acts = $("#breakActs");
  b.state = state;
  breakMsg();
  const close = (label = "Close", cls = "plain") => `<button class="${cls}" data-b="close">${label}</button>`;
  if (state === "nokey") {
    body.innerHTML = `<p>Break it down uses an AI helper to suggest tiny first steps.</p><p class="muted">${esc(noKeyText())}</p>`;
    acts.innerHTML = close("OK", "go");
  } else if (state === "intro") {
    body.innerHTML = `<p>Break it down asks an AI helper (Claude, by Anthropic) for a few tiny first steps. You pick the ones to keep.</p>
      <p class="muted">${esc(BREAK_SENT)}</p><p class="muted">You can turn the AI helpers off in Settings → Focus.</p>`;
    acts.innerHTML = `<button class="go" data-b="ask">Suggest steps</button>${close("Not now")}`;
  } else if (state === "loading") {
    body.innerHTML = `<p role="status"><span class="spin" aria-hidden="true"></span>Thinking of tiny first steps…</p><p class="muted">${esc(BREAK_SENT)}</p>`;
    acts.innerHTML = close("Cancel");
  } else if (state === "error") {
    body.innerHTML = `<p class="err" role="alert">${esc(info)}</p>`;
    acts.innerHTML = `<button class="go" data-b="retry">Try again</button>${close()}`;
  } else if (state === "support") {
    body.innerHTML = `<div class="bnote care" role="note"><p>${esc(info)}</p></div>`;
    acts.innerHTML = close("OK", "go");
  } else if (state === "review") {
    drawBreakSteps();
    acts.innerHTML = reviewActs() + close();
  } else if (state === "saved") {
    const first = (info.steps || []).find((s) => !s.done);
    body.innerHTML = `<div class="bnote" role="status"><p><b>Saved ✓</b> It has ${plural((info.steps || []).length, "tiny step")} now.</p>
      ${first ? `<p>Start with: ${esc(first.text)}</p>` : ""}</div>${info.cut ? `<p class="muted">A task holds 12 steps, so the last ones were left off.</p>` : ""}`;
    acts.innerHTML = `${close("Done", "go")}<button class="plain" data-b="print">Print just this</button><button class="plain" data-b="undo">Undo</button>`;
  } else if (state === "copied") {
    body.innerHTML = `<div class="bnote" role="status"><p><b>Added to My list ✓</b> “${esc(info.content)}”, with ${plural((info.steps || []).length, "tiny step")}.</p>
      <p class="muted">The task in ${esc(b.task?.app || "your linked app")} is still there.</p></div>`;
    acts.innerHTML = `${close("Done", "go")}<button class="plain" data-b="mylist">Open My list</button>`;
  } else if (state === "printed") {
    body.innerHTML = `<h3 tabindex="-1">${esc(resultHeading(info))}</h3>${ticketResult(info)}`;
    acts.innerHTML = close("Done", "go");
  }
  const focus = state === "review" ? body.querySelector("input[type=checkbox]") : state === "printed" ? body.querySelector("h3") : acts.querySelector("button");
  focus?.focus();
}

// Every request from the sheet: its buttons wait, and a problem is said inside the sheet.
async function breakDo(button, label, fn) {
  const b = brk, buttons = $$("#breakActs button, #breakBody button");
  buttons.forEach((x) => { x.disabled = true; });
  const was = button.textContent;
  button.textContent = label; button.setAttribute("aria-busy", "true");
  breakMsg();
  try { await fn(b); }
  catch (e) {
    if (brk !== b) return;
    buttons.forEach((x) => { if (x.isConnected) x.disabled = false; });
    button.textContent = was; button.removeAttribute("aria-busy");
    if (e.message === TASK_GONE) b.changed = true;
    breakMsg(errText(e), "err");
    if (b.state === "review") countBreak();
  }
}

async function saveBreak(button) {
  await breakDo(button, "Saving…", async (b) => {
    const old = (b.task.steps || []).map((s) => ({ text: s.text, done: !!s.done }));
    const fresh = keptSteps().map((text) => ({ text, done: false }));
    const all = b.replace || !old.length ? fresh : [...old, ...fresh];
    const r = await api(`/list/${encodeURIComponent(b.task.id)}`, { method: "PATCH", json: { steps: all.slice(0, 12) } });
    b.changed = true;
    if (brk !== b) return loadList();
    b.undo = old;
    b.slides = r.slides_was;  // new steps clear its slide count: Undo puts it back
    drawBreak("saved", { steps: r.steps, cut: all.length > 12 });
  });
}
async function undoBreak(button) {
  await breakDo(button, "Undoing…", async (b) => {
    const back = { steps: b.undo || [] };
    if (b.slides != null) back.slides = b.slides;
    const r = await api(`/list/${encodeURIComponent(b.task.id)}`, { method: "PATCH", json: back });
    if (brk !== b) return loadList();
    b.task.steps = r.steps;
    drawBreak("review");
    breakMsg("Undone. Its steps are back the way they were.", "muted");
  });
}
async function copyBreak(button) {
  await breakDo(button, "Adding…", async (b) => {
    const content = `${b.task.content.slice(0, 492)}: steps`;
    const steps = keptSteps().map((text) => ({ text, done: false }));
    // A fresh start: a task that was waiting comes over for today, not already late (and not a slug).
    const due = b.task.due_date && b.task.due_date < today() ? today() : (b.task.due_date || null);
    const r = await api("/list", { method: "POST", json: { content, steps, due_date: due } });
    b.changed = true;
    if (brk !== b) return loadList();
    drawBreak("copied", r);
  });
}
async function printBreak(button) {
  applyDry();  // honours paused printing, preview only and "no printer yet"
  const dry_run = $("#dry").checked;
  if (!dry_run && !confirm("Send this to the printer?")) return;
  await breakDo(button, dry_run ? "Making preview…" : "Printing…", async (b) => {
    const body = { task_id: b.task.id, dry_run };
    if (!b.task.local) body.steps = keptSteps();  // on the card only: never sent to the linked app
    const r = await api("/print/focus", { method: "POST", json: body });
    if (brk === b) drawBreak("printed", r);
    loadGuide();
  });
}

$("#breakDialog").addEventListener("click", (e) => {
  const button = e.target.closest("[data-b]"), b = brk;
  if (!button || !b) return;
  const what = button.dataset.b;
  if (what === "close") $("#breakDialog").close();
  else if (what === "ask") { breakSeenNow(); askBreak(); }
  else if (what === "retry") askBreak(b.hint);
  else if (what === "again") askBreak($("#bHint").value.trim());
  else if (what === "add") {
    b.steps.push({ text: "", keep: true });
    drawBreakSteps(); countBreak();
    $("#breakBody .bsteps li:last-child textarea")?.focus();
  }
  else if (what === "save") saveBreak(button);
  else if (what === "undo") undoBreak(button);
  else if (what === "copy") copyBreak(button);
  else if (what === "print") printBreak(button);
  else if (what === "mylist") { $("#breakDialog").close(); goto("mylist"); }
  else if (what === "use") {
    editStepsSet(keptSteps(), b.replace || !hadSteps());
    $("#breakDialog").close();
    toast("Added to the steps. Tap Save to keep them.", 3500);
  }
});
$("#breakDialog").addEventListener("change", (e) => {
  const li = e.target.closest(".bsteps li"), b = brk;
  if (!b) return;
  if (e.target.name === "bhow") b.replace = e.target.value === "replace";
  if (li && e.target.type === "checkbox") {
    b.steps[li.dataset.i].keep = e.target.checked;
    li.classList.toggle("off", !e.target.checked);
    countBreak();
  }
});
$("#breakDialog").addEventListener("input", (e) => {
  const li = e.target.closest(".bsteps li");
  if (!li || !brk || e.target.tagName !== "TEXTAREA") return;
  brk.steps[li.dataset.i].text = e.target.value.replace(/\s*\n\s*/g, " ");
  fitText(e.target);
  countBreak();
});
$("#breakDialog").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !(e.target.id === "bHint" || e.target.tagName === "TEXTAREA")) return;
  e.preventDefault();  // a step is one line: Enter goes to the next one
  if (e.target.id === "bHint") return askBreak(e.target.value.trim());
  const next = e.target.closest("li")?.nextElementSibling?.querySelector("textarea");
  if (next) next.focus(); else $("#breakBody [data-b=add]")?.click();  // Enter on the last step adds one
});
$("#breakDialog").addEventListener("close", () => {
  const b = brk;
  brk = null;
  if (!b) return;
  b.ctl?.abort();
  $("#breakBody").innerHTML = ""; $("#breakActs").innerHTML = ""; breakMsg();
  if (b.changed) { loadList(); if (b.from === "slug") loadSlugs(); }
  let back = b.opener?.isConnected && !b.opener.hidden && !b.opener.disabled ? b.opener : null;
  if (!back) { back = $("#slugCard:not([hidden]) h2") || $("section.on h2"); if (back) back.tabIndex = -1; }
  back?.focus();
});

$("#nowBreak").onclick = () => {
  const id = $("#nowCard").dataset.id;
  if (id) openBreak(id, $("#nowText").textContent, "now", $("#nowBreak"));
};
// From the task dialog: a new task, or changed words, are saved first, so the steps are for the task
// as it reads now (the dialog stays open, and "Use N steps" fills its step list as before).
$("#tdBreak").onclick = async () => {
  if (!editing || tdBusy()) return;
  if (!$("#tdContent").value.trim()) return $("#taskForm").reportValidity();
  let said = "";
  if (!editing.id || tdDirty()) {
    const creating = !editing.id;
    try { await tdHold(saveEditing); } catch (err) { return listError(err, editing?.id); }
    if (creating) { $("#tdTitle").textContent = "Edit task"; $("#tdDelete").hidden = false; }
    focusLabel();
    loadList();
    said = creating ? "Added to My list" : "Your changes are saved";
  }
  openBreak(editing.id, editing.content, "edit", $("#tdBreak"));
  if (said) toast(said);  // said in the sheet, so it's seen
};
$("#slugList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-slug-break]"), s = b?.closest("[data-task]");
  if (s) openBreak(s.dataset.task, s.dataset.content, "slug", b);
});

// Look back on my week: only when asked, and the journal notes go only when ticked.
let lookN = 0, lookCtl = null, lookText = "";
function lookPrivacy() {
  $("#lookPrivacy").textContent = `This sends your week's task names${$("#lookNotes").checked ? " and your journal notes" : ""} `
    + "to Anthropic's AI to write the note. Nothing is kept by Next Box unless you save it.";
}
$("#lookNotes").addEventListener("change", lookPrivacy);
function lookDone() { $("#lookGo").disabled = false; $("#lookGo").removeAttribute("aria-busy"); }
$("#lookGo").onclick = async () => {
  const n = ++lookN, out = $("#lookOut"), go = $("#lookGo");
  lookCtl?.abort();
  lookCtl = new AbortController();
  go.disabled = true; go.setAttribute("aria-busy", "true");
  out.innerHTML = `<p role="status"><span class="spin" aria-hidden="true"></span>Looking back on your week…</p>
    <button class="plain small" data-look="cancel">Cancel</button>`;
  out.querySelector("button").focus();
  try {
    const r = await api("/journal/look-back", { method: "POST", json: { include_notes: $("#lookNotes").checked }, signal: lookCtl.signal });
    if (n !== lookN) return;
    lookDone();
    drawLook(r);
  } catch (e) {
    if (n !== lookN || e.name === "AbortError") return;
    lookDone();
    out.innerHTML = `<p class="err" role="alert">${esc(errText(e))}</p>`;
    go.focus();
  }
};
const monthDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
function drawLook(r) {
  const out = $("#lookOut");
  lookText = "";
  if (r.empty) {
    out.innerHTML = `<p class="muted" role="status">Nothing from this week to look back on yet. ${r.has_notes
      ? "Tick “Include my journal notes” to use what you wrote, or tick off one small thing first."
      : "Tick off one small thing or write a note, then come back."}</p>`;
    return $("#lookGo").focus();
  }
  if (r.concern) {
    out.innerHTML = `<div class="coachnote care" role="note"><h3 tabindex="-1">Before anything else</h3><p>${esc(r.support)}</p></div>
      <div class="row"><button class="plain" data-look="hide">Close</button></div>`;
  } else {
    const n = r.note;
    lookText = r.text;
    out.innerHTML = `<div class="coachnote"><h3 tabindex="-1">Your week, ${esc(monthDay(r.week.first))} to ${esc(monthDay(r.week.last))}</h3>
        <p>${esc(n.went_well)}</p>${n.slug ? `<p>${esc(n.slug)}</p>` : ""}
        ${n.suggestion ? `<p><b>One tiny thing for next week:</b> ${esc(n.suggestion)}</p>` : ""}</div>
      <div class="row"><button class="go" data-look="save">Save to my journal</button><button class="plain" data-look="hide">Close</button>
        <span data-look-msg role="status"></span></div>`;
  }
  out.querySelector("h3").focus();
}
$("#lookOut").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-look]");
  if (!b) return;
  if (b.dataset.look === "cancel") { lookN++; lookCtl?.abort(); $("#lookOut").innerHTML = ""; lookDone(); $("#lookGo").focus(); }
  else if (b.dataset.look === "hide") { $("#lookOut").innerHTML = ""; lookText = ""; $("#lookGo").focus(); }
  else if (b.dataset.look === "save" && lookText) {
    const msg = $("#lookOut [data-look-msg]");
    b.disabled = true; msg.className = "muted"; msg.textContent = "Saving…";
    try {
      await api("/journal", { method: "POST", json: { notes: lookText } });
      lookText = "";
      b.remove(); msg.className = "ok"; msg.textContent = "Saved to today's journal ✓";
      $("#lookOut [data-look=hide]")?.focus();
      loadJournal();
    } catch (err) { b.disabled = false; msg.className = "err"; msg.textContent = errText(err); }
  }
});

// "Ask for ClickUp" (Settings → Linked app → Other apps): the feedback form, filled in as an idea.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-ask-app]");
  if (!b) return;
  $(".seg [data-mode=guided]").click();
  pick("#fbType", $("#fbType [data-v=idea]"));
  $("#fbTrying").value = `Link ${b.dataset.askApp} to Next Box`;
});


// ---------------------------------------------------------------- print on paper (any printer)
const PAPERS =[["letter", "Letter"], ["a4", "A4"], ["a5", "A5"], ["half_letter", "Half letter"]];
function paperButton(id) {
  const size = me?.prefs?.paper_size || "letter";
  return `<div class="row paperrow"><a class="go small" style="text-decoration:none" data-paper-link href="/printed/${encodeURIComponent(id)}/pdf?paper=${size}" target="_blank" rel="noopener">Print on paper</a>
    <select data-paper aria-label="Paper size">${PAPERS.map(([v, n]) => `<option value="${v}"${v === size ? " selected" : ""}>${n}</option>`).join("")}</select>
    ${saveImageLink(id, "plain small")}
    <span class="muted">Any printer: opens a page, then use your phone's or computer's Print.</span></div>`;
}
// Save label image: the ticket's picture as a file (stickers: all of them in one tall image), to open in a label
// printer's own app (like Brother iPrint&Label) or print. Only a download: it never prints anything.
function saveImageLink(id, cls) {
  const name = `next-box-${id}.png`;
  return `<a class="${cls}" data-save-png href="${png(id)}?download=1" download="${esc(name)}"
    aria-label="Save label image of ticket #${esc(id)}">Save label image</a>`;
}
document.addEventListener("change", (e) => {
  if (!e.target.matches?.("select[data-paper]")) return;
  const link = e.target.closest(".paperrow").querySelector("[data-paper-link]");
  const u = new URL(link.href, location.href);  // a4, a5: the size names have digits in them
  u.searchParams.set("paper", e.target.value);
  link.href = u.href;
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
// Nor with Esc or the phone's back gesture, without asking. Esc is handled as a key, as in the task
// dialog (W3-02): a second "Cancel" in a row closed it anyway. "OK" clears what was typed, as it says;
// closed any other way, it's kept for the next time Send feedback opens.
function fbAsk() {
  if (!$("#fbDone").hidden || !$$("#fbDialog textarea").some((t) => t.value.trim())) return true;
  if (!confirm("Close without sending? What you typed will be lost.")) return false;
  $$("#fbDialog textarea").forEach((t) => { t.value = ""; });
  return true;
}
let fbKeep = null;
$("#fbDialog").addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  e.preventDefault();
  if (fbAsk()) $("#fbDialog").close();
});
$("#fbDialog").addEventListener("cancel", (e) => {
  if (!fbAsk()) { e.preventDefault(); if (!e.cancelable) fbKeep = document.activeElement; }
});
$("#fbDialog").addEventListener("close", () => {
  if (!fbKeep) return;
  const el = fbKeep; fbKeep = null;
  $("#fbDialog").showModal(); if ($("#fbDialog").contains(el)) el.focus();
});
// What deleting your account removes, and how to keep a copy first.
const DELETE_WARNING = `<p class="err">This deletes your account, your list, tags, journal pages and journal photos, tickets and history from Next Box. It can't be undone. Apps you linked (like Todoist) aren't touched.</p>
  <p class="muted">Want a copy first? <a href="/auth/export?format=zip" data-export-data>Download my data</a>: one .zip file with your list, settings, journal pages and photos, and ticket images.</p>
  <p class="muted"><button class="linkish" data-journal-export>Download your journal first</button>: a folder for each day, or a PDF you can print.</p>`;
$("#meDialog").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-me]");
  if (!b) return;
  const what = b.dataset.me, panel = $("#mePanel");
  if (what === "settings") { $("#meDialog").close(); return goto("settings"); }
  if (what === "profile") { $("#meDialog").close(); return goto("profile"); }
  if (what === "feedback") { $("#meDialog").close(); return document.querySelector("[data-feedback]")?.click(); }
  if (what === "refresh") return location.reload();
  if (what === "export") return exportMyData();
  if (what === "signout") return signOut(panel);
  if (what === "delete") {
    panel.innerHTML = `${DELETE_WARNING}
      <div class="field"><label for="meDelPw">Your password</label><input type="password" id="meDelPw" autocomplete="current-password"></div>
      <div class="field"><label for="meDelName">Type your username (${esc(me.username)})</label><input type="text" id="meDelName" autocapitalize="none"></div>
      <div class="row"><button class="go warn small" id="meDelGo">Delete my account forever</button><span class="err" id="meMsg" role="alert"></span></div>`;
    $("#meDelGo").onclick = async () => {
      try { await api("/auth/delete-account", { method: "POST", json: { password: $("#meDelPw").value, confirm: $("#meDelName").value } });
            startOver("Your account was deleted."); }
      catch (err) { delAccountErr(err, $("#meDelPw"), $("#meDelName"), $("#meMsg")); }
    };
  }
  panel.scrollIntoView({ behavior: smooth(), block: "nearest" });
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
  document.documentElement.toggleAttribute("data-reading", !!me.prefs.reading_mode);  // Easy-to-read text
  // The Help, Privacy, Terms and About pages (pages.js) read these too, like the Theme.
  try { if (me.prefs.reading_mode) localStorage.setItem("nb.reading", "1"); else localStorage.removeItem("nb.reading"); } catch {}
  try { if (size === "normal") localStorage.removeItem("nb.textSize"); else localStorage.setItem("nb.textSize", size); } catch {}
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
$("#pForm").onsubmit = (e) => { e.preventDefault(); hold($("#pSave"), saveProfile); };
async function saveProfile() {
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
}
$("#pForm").addEventListener("input", () => { $("#pMsg").textContent = ""; });
$("#pDelete").onclick = () => {
  $("#pDeletePanel").innerHTML = `${DELETE_WARNING}
    <div class="field"><label for="pDelPw">Your password</label><input type="password" id="pDelPw" autocomplete="current-password"></div>
    <div class="field"><label for="pDelName">Type your username (${esc(me.username)})</label><input type="text" id="pDelName" autocapitalize="none"></div>
    <div class="row"><button class="go warn" id="pDelGo">Delete my account forever</button><span class="err" id="pDelMsg" role="alert"></span></div>`;
  $("#pDelGo").onclick = async () => {
    try { await api("/auth/delete-account", { method: "POST", json: { password: $("#pDelPw").value, confirm: $("#pDelName").value } });
          startOver("Your account was deleted."); }
    catch (err) { delAccountErr(err, $("#pDelPw"), $("#pDelName"), $("#pDelMsg")); }
  };
};
// Delete my account didn't go: the message is said aloud (role=alert), and the box it's about is marked,
// tied to it and focused, so a screen reader knows what to fix (W3-05).
function delAccountErr(err, pw, name, msg) {
  msg.textContent = errText(err);
  const bad = /password didn't match/i.test(err.message) ? pw : /username/i.test(err.message) ? name : null;
  for (const f of [pw, name]) {
    if (f === bad) { f.setAttribute("aria-invalid", "true"); f.setAttribute("aria-describedby", msg.id); }
    else { f.removeAttribute("aria-invalid"); f.removeAttribute("aria-describedby"); }
  }
  bad?.focus();
}
$("#setJump").onclick = (e) => {
  const b = e.target.closest("[data-jump]"), card = b && document.getElementById(b.dataset.jump);
  if (!card) return;
  card.scrollIntoView({ behavior: smooth(), block: "start" });
  const h = card.querySelector("h2");  // keyboard and screen readers carry on inside the card
  if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
};
// The row's height (it wraps with big text): whatever you Tab to in Settings scrolls clear of it (W3-03, index.html).
if (window.ResizeObserver) new ResizeObserver(() => {
  if ($("#setJump").offsetHeight) document.documentElement.style.setProperty("--jumpH", `${$("#setJump").offsetHeight}px`);
}).observe($("#setJump"));


// ---------------------------------------------------------------- keyboard
// Enter in a one-line box does what its button does, as it already does in forms (sign in, tags, profile).
const ENTER_GOES = { scanLabel: "#scanGo", ivNote: "#ivGo", nuName: "#nuGo", snName: "#snGo", snEmail: "#snGo",
                     announceText: "#announceGo", evUser: "#evGo", npName: "#npGo", npAddress: "#npGo", udEmail: "#udSave" };
document.addEventListener("keydown", (e) => {
  const el = e.target;
  if (e.key !== "Enter" || e.isComposing || el.tagName !== "INPUT" || el.closest("form") || ["checkbox", "radio", "file"].includes(el.type)) return;
  const btn = ENTER_GOES[el.id] ? $(ENTER_GOES[el.id]) : el.closest("#calFields") ? $("#calGo") : el.closest("#provFields") ? $("#provGo") : null;
  if (btn && !btn.disabled && !btn.hidden) { e.preventDefault(); btn.click(); }
});
// My list: a row reached with Tab is scrolled clear of the sticky print bar at the bottom.
$("#listBody").addEventListener("focusin", (e) => {
  const bar = $("#mylist .printbar");
  if (!bar || e.target.closest(".printbar")) return;
  if (e.target.getBoundingClientRect().bottom > bar.getBoundingClientRect().top) e.target.scrollIntoView({ block: "center" });  // at once, like Tab
});


// ---------------------------------------------------------------- phone navigation, offline
$$(".bottombar [data-go]").forEach((b) => b.onclick = () => $(`#topnav button[data-tab=${b.dataset.go}]`).click());
$("#moreBtn").onclick = () => {
  if (!me) return;
  const tabs = $$("#topnav button").filter((b) => !b.hidden && !["mylist", "print", "scan", "journal"].includes(b.dataset.tab));
  const icons = { printed: "🧾", board: "💬", profile: "👤", settings: "⚙️", admin: "🛠️", superusers: "👥", usage: "📊", debug: "🐞" };
  const here = $("#topnav button.on")?.dataset.tab;
  $("#moreMenu").innerHTML = tabs.map((b) => `<button data-more="${b.dataset.tab}"${b.dataset.tab === here ? ` aria-current="page"` : ""}><span aria-hidden="true">${icons[b.dataset.tab] || "•"}</span> ${esc(b.textContent)}</button>`).join("");
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
  toast(msg === OFFLINE || msg.includes("fetch") || msg.includes("NetworkError") ? OFFLINE : "Something went wrong. Try again.", { error: true });
});


// ---------------------------------------------------------------- FE: the routine card
// At the top of My list: "Print today's label" from the plan time (until it's made), "How did today go? Snap
// your pages" from the look-back time, and "You finished N today ✓" once they're snapped. Worked out here from
// this routine day's tickets and the last read-back; nothing is sent, and the print button is the same Today's
// label button as My list's (a preview, or "Send this to the printer?"): it never prints by itself.
let routineData = null;  // { printed: this routine day's tickets, scans: the last read-back }, null until loaded (or if that failed)
let routineN = 0;
let routineMem = null;   // what "Not today" hid, for a device that keeps nothing (a private window)
// The routine day starts halfway between the look-back time and the next plan time (1:00 with 8:00 and 18:00),
// so a night owl's day (plan 20:00, look back 6:00) is one day across midnight: a label made at 20:30 is still
// today's at 0:30, and "Not today" lasts until the next one. From its start until the plan time the card is quiet.
function routineDay() {
  const p = me?.prefs || {}, d = new Date();
  const plan = schedMinutes(p.routine_morning || "08:00"), look = schedMinutes(p.routine_evening || "18:00");
  const edge = (look + Math.floor(((plan - look + 1440) % 1440) / 2)) % 1440;
  const start = new Date(d);
  start.setHours(Math.floor(edge / 60), edge % 60, 0, 0);
  if (start > d) start.setDate(start.getDate() - 1);
  const into = (min) => (min - edge + 1440) % 1440;  // minutes since the routine day started
  const at = into(d.getHours() * 60 + d.getMinutes()), planAt = into(plan), lookAt = look === plan ? 1440 : into(look);
  const day = start.toLocaleDateString("en-CA"), two = (n) => String(n).padStart(2, "0");
  // `since`: as the server writes created_at (its local time, the same clock today() assumes)
  return { day, since: `${day}T${two(start.getHours())}:${two(start.getMinutes())}:00`, morning: at >= planAt && at < lookAt, evening: at >= lookAt };
}
async function loadRoutine() {
  if (!me) return;
  const n = ++routineN, since = routineDay().since;
  let got = null;
  try {  // all of this routine day's tickets, so stickers or one-task prints made after the label don't hide it
    const [printed, scans] = await Promise.all([api(`/printed?since=${encodeURIComponent(since)}&limit=100`), api("/scans?limit=1")]);
    got = { printed, scans };
  } catch {}  // a help, never an error: the card just steps aside
  if (n !== routineN || !me) return;
  routineData = got;
  routineRender();
}
const routineKey = () => (me ? `nb.routineHide:${me.username}` : "");
function routineHidden() {  // the prompts put away this routine day with "Not today" or "Hide"
  let v = routineMem;
  try { v = JSON.parse(localStorage.getItem(routineKey()) || "null") || routineMem; } catch {}
  return v && v.day === routineDay().day && Array.isArray(v.states) ? v.states : [];
}
function routineSetHidden(states) {
  routineMem = { day: routineDay().day, states };
  try { states.length ? localStorage.setItem(routineKey(), JSON.stringify(routineMem)) : localStorage.removeItem(routineKey()); } catch {}
}
// Which prompt shows now, and its words.
function routineView() {
  const p = me?.prefs || {};
  if (!me || p.routine_card === false || !routineData) return null;
  const now = schedNowMin(), { since, morning, evening } = routineDay();
  const label = routineData.printed.find((t) => t.kind === "today" && (t.created_at || "") >= since && !t.failed);
  // A snap counts once it's of this routine day: after its label was made, or in the evening without one.
  const snap = routineData.scans.find((s) => (s.created_at || "") >= since && (!label || s.created_at >= label.created_at));
  // Something for the label to print (it prints today's list, by the clock).
  const due = [...listData.tasks.filter((t) => !t.done), ...(listData.linked || [])].some((t) => t.due && t.due.date.slice(0, 10) <= today());
  const journal = p.journal_mode !== false;
  let state = null;
  if (snap && (label || evening)) state = "finished";
  else if (!label && morning && lastLoad.at && due) state = "print";
  else if (evening && !snap && (label || (journal && routineData.scans.length))) state = "snap";
  if (!state || routineHidden().includes(state)) return null;
  if (state === "print") return { state, kick: `Plan your day · ${fmtClock(p.routine_morning || "08:00")}`,
    head: `${now < 12 * 60 ? "Good morning. " : ""}Print today's label`,
    sub: journal ? "Today's tasks on one label, for the left page of your journal." : "Today's tasks on one label.",
    acts: `<button class="go" data-routine="print">${$("#dry").checked ? "Preview today's label" : "Print today's label"}</button>
      <button class="plain" data-routine="hide">Not today</button>` };
  if (state === "snap") return { state, kick: `Snap your pages · ${fmtClock(p.routine_evening || "18:00")}`,
    head: "How did today go?",
    sub: journal ? "Tick your label, write a few notes, then take one photo of both pages." : "Tick your label with a pen, then take a photo of it.",
    acts: `<button class="go" data-routine="snap">Snap my pages</button><button class="plain" data-routine="hide">Not today</button>` };
  // Finished this routine day (a night owl's day crosses midnight), the snap's own ✓ marks included.
  const n = listData.done_since ?? listData.done_today ?? 0;
  return { state, kick: "Snap your pages", head: n ? `You finished ${n} today ✓` : "Pages snapped ✓",
    sub: snap.journal_id ? (snap.day && snap.day !== today() ? "Your page is in your journal." : "Today's page is in your journal.") : "",
    acts: `${journal ? `<button class="plain" data-go-tab="journal">Open Journal</button>` : ""}<button class="plain" data-routine="hide">Hide</button>` };
}
function routineRender() {
  const card = $("#routineCard"), v = me ? routineView() : null;
  card.hidden = !v;
  card.dataset.state = v ? v.state : "";
  if (!v) { routineRender.html = ""; return; }
  card.classList.toggle("finished", v.state === "finished");
  const html = [v.kick, v.head, v.sub, v.acts].join("\n");
  if (routineRender.html === html) return;  // nothing new: what you're reading or have focused stays put
  const had = card.contains(document.activeElement) ? document.activeElement.dataset.routine : null;
  routineRender.html = html;
  $("#routineKick").textContent = v.kick;
  $("#routineHead").textContent = v.head;
  $("#routineSub").textContent = v.sub;
  $("#routineActs").innerHTML = v.acts;
  if (had) $(`#routineActs [data-routine="${had}"]`)?.focus();
}
$("#routineActs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-routine]"), state = $("#routineCard").dataset.state;
  if (!b || !state) return;
  if (b.dataset.routine === "print") return printFrom("/print/today", { source: "manual" }, b);  // My list's own Today's label: a preview, or "Send this to the printer?"
  if (b.dataset.routine === "snap") { goto("scan"); setTimeout(() => $("#snapCam").focus(), 200); return; }
  routineSetHidden([...routineHidden(), state]);
  routineRender();
  ($("#nowCard > .row button:not([hidden])") || $("#listTitle")).focus({ preventScroll: true });
  snack({ print: "OK, no label today.", snap: "OK. See you tomorrow.", finished: "Hidden for today." }[state], () => {
    routineSetHidden(routineHidden().filter((x) => x !== state));
    routineRender();
    $("#routineHead").focus({ preventScroll: true });
  });
});
$("#dry").addEventListener("change", routineRender);  // "Preview today's label" or "Print today's label"
function routineForget() { routineData = null; routineMem = null; routineRender.html = ""; $("#routineCard").hidden = true; }


// ---------------------------------------------------------------- FE: focus timer on Just one thing
// "Start 5 min" (or the task's own estimate) puts a countdown ring on the card, with Pause and Stop. The time
// comes from when it started, so switching tabs, locking the phone or reloading doesn't lose it (it's kept on
// this device for this person: the task's id and times, never its words). At the end: a soft chime if sound is
// on (it needs the tap that started it), and Keep going / Done ✓ / Not now. No motion with reduced motion.
let timerState = null;   // { id, total, start, left, paused, ended, day } in ms (day: when it was paused)
let timerIv = null, timerTo = null, timerAudio = null, timerSaidMinute = false;
const TIMER_RING = 2 * Math.PI * 52;  // the ring's length (r = 52 in its 120×120 box)
const timerKey = () => (me ? `nb.timer:${me.username}` : "");
function timerSave() {
  try { const k = timerKey(); if (k) timerState ? localStorage.setItem(k, JSON.stringify(timerState)) : localStorage.removeItem(k); } catch {}
}
// One that ran out over 30 minutes ago (start + total is when: Resume and Keep going move the start), open
// page or not, or one paused on an earlier day, is let go: no "Time's up" waiting on every screen next morning.
function timerStale(s) {
  if (!s) return false;
  if (s.paused) return (s.day || new Date(s.start).toLocaleDateString("en-CA")) !== today();
  return Date.now() - (s.start + s.total) > 30 * 60000;
}
function timerLoad() {  // once per person: a timer that was running when the page closed carries on
  if (!me || timerLoad.for === me.username) return;
  timerLoad.for = me.username;
  try {
    const v = JSON.parse(localStorage.getItem(timerKey()) || "null");
    if (v && typeof v.id === "string" && v.total > 0 && v.total <= 3 * 3600000 && Number.isFinite(v.start)) timerState = v;
  } catch {}
  if (!timerState) return;
  if (timerStale(timerState)) { timerState = null; timerSave(); return; }  // long ago: let it go quietly
  if (!timerState.paused && !timerState.ended && timerLeft() <= 0) {  // it ran out while Next Box was closed
    timerState = { ...timerState, ended: true, left: 0 };
    timerSave();
  }
  timerRun();
}
function timerDrop() {  // let a stale one go, quietly (no message), with the keyboard kept on the card or the note
  const inCard = $("#nowCard").contains(document.activeElement), inBar = $("#remindList").contains(document.activeElement);
  timerClear(); renderNow();
  $("#timerLive").textContent = "";
  if (inCard) timerFocusAfter(); else if (inBar) remindFocusAfter();
}
const timerLeft = () => (!timerState ? 0 : timerState.ended ? 0 : timerState.paused ? timerState.left : timerState.total - (Date.now() - timerState.start));
const timerFmt = (ms) => { const s = Math.max(0, Math.ceil(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const timerEst = (t) => (t && t.minutes >= 1 && t.minutes <= 90 && t.minutes !== 5 ? t.minutes : 0);  // the task's own estimate, as a second choice
function timerTask(open) {  // the task the timer is for, while it's still on today's list
  timerLoad();
  if (!timerState) return null;
  const t = open.find((x) => x.id === timerState.id);
  return t && !skipped.has(t.id) && (!t.due || t.due.date.slice(0, 10) <= today()) ? t : null;
}
function timerSync(t) {
  if (!me) return;
  if (lastLoad.at && timerState && (!t || t.id !== timerState.id)) timerClear();  // done, moved or set aside: the timer goes too
  timerDraw(t);
}
function timerDraw(t) {
  const on = !!(timerState && t && t.id === timerState.id);
  $("#timerBox").hidden = !on;
  $("#timerRow").hidden = on || !t || nowAsk === t.id;
  $("#nowCard > .row").hidden = on && !!timerState.ended;  // time's up: its three choices are the card's buttons for now
  if (!on) {
    const est = timerEst(t);
    $("#timerGoEst").hidden = !est;
    if (est) { $("#timerGoEst").textContent = `or ${est} min`; $("#timerGoEst").setAttribute("aria-label", `or ${est} min: start a focus timer for your estimate`); }
    return;
  }
  const s = timerState, step = String(t.id).startsWith("nb:") && (t.steps || []).some((x) => !x.done);
  $("#timerBox").classList.toggle("paused", !!s.paused);
  $("#timerBox").classList.toggle("ended", !!s.ended);
  $("#timerRun").hidden = !!s.ended;
  $("#timerEnd").hidden = !s.ended;
  $("#timerPause").textContent = s.paused ? "Resume" : "Pause";
  $("#timerSay").textContent = s.ended ? "Time's up. How did it go?" : s.paused ? "Paused. Carry on when you're ready." : "Just this one thing for now.";
  $("#timerMore").textContent = `Keep going (${Math.round(s.total / 60000)} min)`;
  $("#timerDone").textContent = step ? "Step done ✓" : "Done ✓";
  timerSoundDraw();
  timerShow();
}
function timerShow() {  // the clock and the ring; every second while it runs
  if (!timerState || $("#timerBox").hidden) return timerCheck();
  const left = Math.max(0, timerLeft()), total = timerState.total;
  $("#timerClock").textContent = timerFmt(left);
  $("#timerUnder").textContent = timerState.ended ? "done ✓" : `of ${Math.round(total / 60000)} min`;
  $("#timerArc").style.strokeDasharray = String(TIMER_RING);
  $("#timerArc").style.strokeDashoffset = String(timerState.ended ? 0 : TIMER_RING * (1 - left / total));  // done: a whole green ring
  if (!timerState.paused && !timerState.ended && left <= 60000 && total > 60000 && !timerSaidMinute) { timerSaidMinute = true; timerSay("1 minute left."); }
  timerCheck();
}
function timerCheck() {
  if (!timerState || timerState.paused || timerState.ended || timerLeft() > 0) return;
  if (timerStale(timerState)) return timerDrop();  // it ran out long ago (a computer that slept): no "Time's up" now
  timerFinish();
}
function timerRun() {  // the second hand while it runs, and one wake-up for the end (that one fires even in the background)
  clearInterval(timerIv); clearTimeout(timerTo); timerIv = timerTo = null;
  if (!timerState || timerState.paused || timerState.ended) return;
  timerIv = setInterval(timerShow, 1000);
  timerTo = setTimeout(timerCheck, Math.max(0, timerLeft()) + 50);
}
function timerSay(msg) { $("#timerLive").textContent = ""; requestAnimationFrame(() => { $("#timerLive").textContent = msg; }); }
function timerStart(min) {
  const id = $("#nowCard").dataset.id;
  if (!id || !(min > 0)) return;
  timerUnlock();  // this tap is what lets the chime play later
  timerState = { id, total: min * 60000, start: Date.now(), left: 0, paused: false, ended: false };
  timerSaidMinute = false;
  timerSave(); timerRun(); renderNow();
  timerSay(`Timer started: ${min} minutes. Just start.`);
  $("#timerPause").focus({ preventScroll: true });
}
function timerFinish() {
  const name = timerName(), wasIn = $("#nowCard").contains(document.activeElement);
  timerState = { ...timerState, ended: true, left: 0 };
  timerSave(); timerRun();
  renderNow();
  timerChime();
  timerSay(`Time's up${name ? `: ${name}` : ""}. Keep going, Done, or Not now.`);
  if (wasIn) $("#timerMore").focus({ preventScroll: true });  // the button you were on has gone: the next choice is here
  remindNotify("Time's up", name || "Your focus timer", "nb-timer");
  remindRender();  // on another screen, the note at the top says so
}
function timerClear() {
  timerState = null; timerSaidMinute = false;
  timerSave(); timerRun();
  $("#timerBox").hidden = true;
  remindRender();
}
function timerName() {
  const t = timerState && [...listData.tasks, ...(listData.linked || [])].find((x) => x.id === timerState.id);
  return t ? shortName(t) : "";
}
function timerForget() {  // signing out: nothing keeps running on screen (the saved one stays for that person)
  timerState = null; timerLoad.for = ""; timerSaidMinute = false;
  clearInterval(timerIv); clearTimeout(timerTo); timerIv = timerTo = null;
  $("#timerBox").hidden = true; $("#timerRow").hidden = true; $("#nowCard > .row").hidden = false;
  $("#timerLive").textContent = "";
}
const timerFocusAfter = () => ($("#timerRow").hidden ? $("#nowCard > .row button:not([hidden])") || $("#listTitle") : $("#timerGo")).focus({ preventScroll: true });

$("#timerGo").onclick = () => timerStart(5);
$("#timerGoEst").onclick = () => timerStart(timerEst([...listData.tasks, ...(listData.linked || [])].find((x) => x.id === $("#nowCard").dataset.id)));
$("#timerPause").onclick = () => {
  if (!timerState || timerState.ended) return;
  if (timerState.paused) {
    timerUnlock();
    timerState = { ...timerState, paused: false, start: Date.now() - (timerState.total - timerState.left) };
    timerSay(`Going again: ${timerFmt(timerLeft())} left.`);
  } else {
    timerState = { ...timerState, paused: true, left: Math.max(0, timerLeft()), day: today() };  // kept for today only
    timerSay(`Paused with ${timerFmt(timerState.left)} left.`);
  }
  timerSave(); timerRun(); renderNow();
};
$("#timerStop").onclick = () => {
  const was = timerState;
  if (!was) return;
  timerClear(); renderNow(); timerFocusAfter();
  snack("Timer stopped", () => { timerState = was; timerSave(); timerRun(); renderNow(); $("#timerPause").focus({ preventScroll: true }); });
};
$("#timerMore").onclick = () => {
  if (!timerState) return;
  timerUnlock();
  timerState = { ...timerState, start: Date.now(), left: 0, paused: false, ended: false };
  timerSaidMinute = false;
  timerSave(); timerRun(); renderNow();
  timerSay(`Another ${Math.round(timerState.total / 60000)} minutes. Keep going.`);
  $("#timerPause").focus({ preventScroll: true });
};
// What the end choices did: in the usual message on My list, or in the note at the top on another screen.
const timerTell = (msg, undo = null) => ($("#mylist").classList.contains("on") ? snack(msg, undo) : remindNote(msg, undo));
async function timerDoneNow(b) {  // the card's own Done (or Step done ✓), for the timer's task, with Undo
  const id = timerState?.id;
  if (!id || b?.getAttribute("aria-disabled") === "true") return;
  const all = [...listData.tasks, ...(listData.linked || [])], t = all.find((x) => x.id === id);
  const i = String(id).startsWith("nb:") ? (t?.steps || []).findIndex((s) => !s.done) : -1;
  b?.setAttribute("aria-disabled", "true");
  try {
    if (i >= 0) {
      const text = t.steps[i].text;
      timerClear();
      if (await tickStep(id, i, true, text)) timerTell("Step done ✓", () => tickStep(id, i, false, text));
      return;
    }
    const r = await api(`/list/${encodeURIComponent(id)}/done`, { method: "POST", json: { due: t?.due?.date?.slice(0, 10) } });
    timerClear();
    if (r.stale) { timerTell(DONE_STALE); await loadList(); return; }
    const canUndo = r.undo != null && !(t && !String(id).startsWith("nb:") && t.due?.is_recurring);  // as My list's Done
    timerTell(`Done ✓ ${t ? shortName(t) : ""}`.trim(), canUndo ? () => undoDone(id, r.undo) : null);
    await loadList();
  } catch (err) { listError(err, id); }
  finally { b?.removeAttribute("aria-disabled"); }
}
$("#timerDone").onclick = async () => { await timerDoneNow($("#timerDone")); renderNow(); timerFocusAfter(); };
function timerNotNow() {
  if (!timerState) return;
  const min = Math.round(timerState.total / 60000);
  timerClear();
  timerTell(me?.prefs?.routine_mode ? "Timer done ✓" : `${plural(min, "minute")} of focus. That counts ✓`);
}
$("#timerNotNow").onclick = () => { timerNotNow(); renderNow(); timerFocusAfter(); };
// The chime: two soft notes, made here (no sound file), and only once a tap has allowed sound.
const timerSoundOn = () => { try { return localStorage.getItem("nb.timerSound") !== "off"; } catch { return true; } };
function timerSoundDraw() {
  const on = timerSoundOn();
  $("#timerSound").setAttribute("aria-pressed", String(on));
  $("#timerSound").innerHTML = `<span aria-hidden="true">${on ? "🔔" : "🔕"} </span>Chime at the end`;
}
$("#timerSound").onclick = () => {
  const on = !timerSoundOn();
  try { localStorage.setItem("nb.timerSound", on ? "on" : "off"); } catch {}
  if (on) timerUnlock();
  timerSoundDraw();
  timerSay(on ? "Chime on." : "Chime off.");
};
function timerUnlock() {
  if (!timerSoundOn()) return;
  try { timerAudio = timerAudio || new (window.AudioContext || window.webkitAudioContext)(); timerAudio.resume?.(); } catch { timerAudio = null; }
}
function timerChime() {
  const a = timerAudio;
  if (!a || !timerSoundOn()) return;
  try {
    a.resume?.();
    const t0 = a.currentTime + 0.05;
    for (const [hz, at] of [[659.25, 0], [880, 0.45]]) {
      const o = a.createOscillator(), g = a.createGain();
      o.type = "sine"; o.frequency.value = hz;
      g.gain.setValueAtTime(0.0001, t0 + at);
      g.gain.exponentialRampToValueAtTime(0.16, t0 + at + 0.04);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 1.6);
      o.connect(g).connect(a.destination);
      o.start(t0 + at); o.stop(t0 + at + 1.7);
    }
  } catch {}
}
// After a reload a running timer has no sound yet: the next tap anywhere allows it again.
for (const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, () => { if (timerState && !timerState.ended && !timerAudio) timerUnlock(); }, true);


// ---------------------------------------------------------------- FE: reminders before timed tasks
// While Next Box is open (any screen), a note at the top N minutes before a task with a time (Settings →
// Focus: 10 by default, or Off), with Done / Tomorrow / Snooze 10m and ✕. Worked out here from My list as
// last loaded: no server timer, no push. With "Show a notification too", the browser shows it as well while
// Next Box is in the background; permission is only asked for when that's turned on.
const REMIND_LATE = 30;        // minutes after its time that a task's reminder still shows
const REMIND_STALE = 5 * 60000;  // a list older than this is fetched again before a reminder is notified
const remindSeen = new Set();  // reminders already announced (and notified) on this visit
const remindShown = new Set(); // notifications this page made (closed again on sign-out)
let remindNoteNow = null;      // { text, undo, id }: what the last tap did, with Undo, for a few seconds
let remindNoteTimer = null, remindMem = null;
const remindKey = () => (me ? `nb.remind:${me.username}` : "");
// Dismissed and snoozed reminders (task ids and times only), kept until the day after the task's time, so one
// put away at 23:55 for 0:15 doesn't come back at midnight.
function remindMemo() {
  let v = remindMem;
  try { v = JSON.parse(localStorage.getItem(remindKey()) || "null") || remindMem; } catch {}
  const since = new Date(Date.now() - 864e5).toLocaleDateString("en-CA");  // yesterday
  const keep = (o) => Object.fromEntries(Object.entries(o && typeof o === "object" ? o : {})
    .filter(([k]) => k.slice(k.lastIndexOf("@") + 1).slice(0, 10) >= since));
  return { off: keep(v?.off), snooze: keep(v?.snooze) };
}
function remindKeep(v) { remindMem = v; try { localStorage.setItem(remindKey(), JSON.stringify(v)); } catch {} }
function remindAt(t) {  // a timed task's time on this device's clock (as My list shows it), else NaN
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(t?.due?.date || "");
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]).getTime() : NaN;
}
// Open tasks with a time, by real time rather than today's date: 0:15 tomorrow is reminded from 23:45 tonight.
const remindTimed = () => [...listData.tasks.filter((t) => !t.done), ...(listData.linked || [])].filter((t) => Number.isFinite(remindAt(t)));
function remindDue() {
  const lead = me?.prefs?.remind_before ?? 10;
  if (!me || !lead || !lastLoad.at) return [];
  const now = Date.now(), memo = remindMemo();
  return remindTimed()
    .map((t) => ({ t, at: remindAt(t), key: `${t.id}@${t.due.date.slice(0, 16)}` }))
    .map((x) => ({ ...x, min: Math.ceil((x.at - now) / 60000) }))  // minutes to go (0 or less: now)
    .filter((x) => x.at - now <= lead * 60000 && now - x.at < REMIND_LATE * 60000 && !memo.off[x.key] && !((memo.snooze[x.key] || 0) > now))
    .sort((a, b) => a.at - b.at);
}
const remindWhen = (x) => (x.min > 0 ? `in ${schedIn(x.min)}` : "now");
function remindRender() {
  const bar = $("#remindBar"), list = $("#remindList");
  if (!me) return remindForget();
  const items = remindDue(), nowId = remindNowSeen();
  // Just one thing's own task, with the card on screen: it's right there with its time and buttons.
  const shown = items.filter((x) => x.t.id !== nowId);
  remindRender.mine = !!$("#nowCard").dataset.id && items.some((x) => x.t.id === $("#nowCard").dataset.id);
  let html = shown.slice(0, 3).map((x) => {
    const name = esc(shortName(x.t)), clock = esc(fmtClock(x.t.due.date.slice(11, 16)));
    return `<div class="remind" data-rkey="${esc(x.key)}" data-id="${esc(x.t.id)}"><span aria-hidden="true">🔔</span>
      <div class="remindtext"><b>${x.min > 0 ? `${schedIn(x.min)}:` : "Now:"}</b> ${name} <span class="when">· ${clock}</span></div>
      <div class="remindacts" role="group" aria-label="Reminder: ${name}">
        <button class="plain small" data-r="done" aria-label="Done: ${name}">Done ✓</button>
        <button class="plain small" data-r="tomorrow" aria-label="Tomorrow: ${name}">Tomorrow</button>
        <button class="plain small" data-r="snooze" aria-label="Snooze 10m: ${name}">Snooze 10m</button></div>
      <button class="linkish rclose" data-r="off" aria-label="Dismiss the reminder for ${name}" title="Dismiss">✕</button></div>`;
  }).join("");
  if (shown.length > 3) html += `<div class="remind"><div class="remindtext">…and ${plural(shown.length - 3, "more")} at a set time. <button class="linkish" data-go-tab="mylist">See My list</button></div></div>`;
  // The focus timer ran out while you're on another screen (or scrolled away from Just one thing).
  if (timerState?.ended && !remindCardSeen()) {
    html = `<div class="remind" data-rkey="timer"><span aria-hidden="true">⏱</span><div class="remindtext"><b>Time's up:</b> ${esc(timerName() || "your one thing")}</div>
      <div class="remindacts" role="group" aria-label="Time's up"><button class="plain small" data-r="timer-more">Keep going</button>
        <button class="plain small" data-r="timer-done">${esc($("#timerDone").textContent)}</button>
        <button class="plain small" data-r="timer-notnow">Not now</button></div></div>` + html;
  }
  if (remindNoteNow) html = `<div class="remind" data-rkey="note"><span aria-hidden="true">✓</span><div class="remindtext">${esc(remindNoteNow.text)}</div>
    ${remindNoteNow.undo ? `<div class="remindacts"><button class="plain small" data-r="undo">Undo</button></div>` : ""}</div>` + html;
  if (list.dataset.html !== html) {  // drawn again only when something changed; focus stays on the same button
    const f = document.activeElement, spot = list.contains(f) ? [f.closest("[data-rkey]")?.dataset.rkey || "", f.dataset.r || ""] : null;
    list.dataset.html = html;
    list.innerHTML = html;
    if (spot) list.querySelector(`[data-rkey="${CSS.escape(spot[0])}"] [data-r="${CSS.escape(spot[1])}"]`)?.focus({ preventScroll: true });
  }
  bar.hidden = !html;
  remindMeasure();
  // Said once (Just one thing's too), and shown by the browser as well when Next Box is in the background, but
  // not from a list that's been sitting there: that waits for the fresh one (it may be done on another device).
  const stale = document.visibilityState !== "visible" && Date.now() - lastLoad.at > REMIND_STALE;
  const fresh = stale ? [] : items.filter((x) => !remindSeen.has(x.key));
  for (const x of fresh) {
    remindSeen.add(x.key);
    remindNotify(x.t.content, `${x.min > 0 ? `In ${schedIn(x.min)}` : "Now"} · ${fmtClock(x.t.due.date.slice(11, 16))}`, `nb-${x.key}`);
  }
  if (fresh.length) $("#remindSay").textContent = fresh.map((x) => `Reminder: ${x.t.content}, ${remindWhen(x)}.`).join(" ");
}
// On phones the note scrolls away with the page (index.html), so it never covers much of a small screen.
const remindSticky = () => getComputedStyle($("#remindBar")).position === "sticky";
// The note's height while it sticks to the top: Settings' "Jump to" row and the bulk bars stick under it
// (--remindH), and whatever you Tab to scrolls clear of it.
const remindCover = () => (!$("#remindBar").hidden && remindSticky() ? $("#remindBar").offsetHeight : 0);
function remindMeasure() {
  const h = remindCover(), root = document.documentElement.style;
  root.setProperty("--remindH", `${h}px`);
  root.scrollPaddingTop = h ? `${h + 12}px` : "";
}
window.addEventListener("resize", remindMeasure);
if (window.ResizeObserver) new ResizeObserver(() => remindMeasure()).observe($("#remindBar"));  // it wraps, or the text size changed
// Just one thing on screen says it already. On phones the note sits right above the card, so on My list that's
// wherever you've scrolled (a line added up there would only push the page about); where the note sticks to the
// top, it's while the card is on screen.
function remindCardSeen() {  // Just one thing's timer is on screen: its own buttons are right there
  if (!$("#mylist").classList.contains("on") || $("#timerBox").hidden) return false;
  if (!remindSticky()) return true;
  const r = $("#timerBox").getBoundingClientRect(), mid = (r.top + r.bottom) / 2;
  return mid > 0 && mid < window.innerHeight;
}
function remindNowSeen() {  // Just one thing's task, while its words are on screen (not under the note), else ""
  const id = $("#nowCard").dataset.id;
  if (!id || !$("#mylist").classList.contains("on")) return "";
  if (!remindSticky()) return id;
  const r = $("#nowText").getBoundingClientRect();
  return r.height && r.bottom > remindCover() && r.top < window.innerHeight ? id : "";
}
let remindScrolling = false;
window.addEventListener("scroll", () => {  // scrolling to or away from the card swaps the note at the top
  if (!(timerState?.ended || remindRender.mine) || remindScrolling) return;
  remindScrolling = true;
  requestAnimationFrame(() => { remindScrolling = false; remindRender(); });
}, { passive: true });
function remindNote(text, undo, id = "") {  // "Done ✓ Call the dentist · Undo" where the reminder was
  clearTimeout(remindNoteTimer);
  remindNoteNow = { text, undo, id };
  $("#remindSay").textContent = text;
  remindRender();
  remindNoteTimer = setTimeout(() => { remindNoteNow = null; remindRender(); }, ["large", "xlarge"].includes(me?.prefs?.text_size) ? 12000 : 8000);
}
function remindFocusAfter() {  // the row you were on has gone: the next one, else this screen's heading
  const next = $("#remindList button");
  if (next) return next.focus({ preventScroll: true });
  const h = $$("main section.on h2").find((x) => x.offsetParent);
  if (h) { if (!h.hasAttribute("tabindex")) h.tabIndex = -1; h.focus({ preventScroll: true }); }
}
$("#remindBar").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-r]");
  if (!b || b.getAttribute("aria-disabled") === "true") return;
  const act = b.dataset.r, row = b.closest("[data-rkey]"), key = row?.dataset.rkey, id = row?.dataset.id;
  const t = [...listData.tasks, ...(listData.linked || [])].find((x) => x.id === id);
  const name = t ? shortName(t) : "";
  const inBar = $("#remindBar").contains(document.activeElement);
  if (act.startsWith("timer-")) {
    if (act === "timer-more") { $("#timerMore").click(); remindNote(`Timer on: ${plural(Math.round((timerState?.total || 0) / 60000), "more minute")}. It's on My list.`); }
    else if (act === "timer-done") await timerDoneNow(b);
    else timerNotNow();
    remindRender();
    if (inBar) remindFocusAfter();
    return;
  }
  if (act === "undo") {
    const undo = remindNoteNow?.undo, back = remindNoteNow?.id;
    clearTimeout(remindNoteTimer); remindNoteNow = null; remindRender();
    if (undo) await undo();  // the list is loaded again before this carries on
    if (inBar) {  // back on the reminder that came back, else the next thing in the note
      const again = back && $(`#remindList [data-id="${CSS.escape(back)}"] [data-r=done]`);
      if (again) again.focus({ preventScroll: true }); else remindFocusAfter();
    }
    return;
  }
  if (key && key.includes("@")) remindUnnotify(`nb-${key}`);  // handled here: its notification goes too
  if (act === "off" || act === "snooze") {
    const memo = remindMemo();
    if (act === "off") { memo.off[key] = true; remindKeep(memo); remindRender(); }
    else {
      memo.snooze[key] = Date.now() + 10 * 60000;
      remindKeep(memo);
      remindSeen.delete(key);  // when it comes back it's said (and notified) again
      setTimeout(remindRender, 10 * 60000 + 200);  // back right on time, not at the next 20 s check
      remindNote(`Snoozed: ${name}. Back in 10 minutes.`, () => {
        const m = remindMemo(); delete m.snooze[key]; remindKeep(m);
        remindSeen.add(key);  // back on screen at once, where you are: no need to say it again
        remindRender();
      }, id);
    }
    if (inBar) remindFocusAfter();
    return;
  }
  await hold(b, async () => {
    try {
      if (act === "done") {
        const r = await api(`/list/${encodeURIComponent(id)}/done`, { method: "POST", json: { due: t?.due?.date?.slice(0, 10) } });
        const canUndo = r.undo != null && !(t && !String(id).startsWith("nb:") && t.due?.is_recurring);  // as My list's Done
        if (r.stale) remindNote(DONE_STALE, null, id);
        else remindNote(`Done ✓ ${name}`, canUndo ? () => undoDone(id, r.undo) : null, id);
      } else if (act === "tomorrow") {
        const r = await api(`/list/${encodeURIComponent(id)}/move`, { method: "POST", json: { to: "tomorrow" } });
        if (r.moved) remindNote(`Moved to tomorrow: ${name}`, () => undoMove(id, r.undo), id);
        else toast(`It's on ${dayWords(r.to)} already`);
      }
      await loadList();
    } catch (err) { listError(err, id); }
  });
  if (inBar) remindFocusAfter();
});
// "Show a notification too": the browser asks for permission only when it's turned on here.
const remindCanNotify = () => "Notification" in window && window.isSecureContext;
const REMIND_NOTIFY_NOTE = $("#remindNotifyNote").textContent;
function remindShowPrefs(p) {
  const box = $("#remindNotify"), note = $("#remindNotifyNote");
  box.checked = !!p.remind_notify;
  box.disabled = !remindCanNotify() && !p.remind_notify;
  let msg = REMIND_NOTIFY_NOTE, bad = false;
  if (!remindCanNotify()) msg = "This browser can't show notifications from Next Box here (they need its https address or the installed app). The note at the top still works.";
  else if (p.remind_notify && Notification.permission === "denied") { msg = "Notifications are blocked for Next Box in this browser's settings. Allow them there, or turn this off."; bad = true; }
  else if (p.remind_notify && Notification.permission === "default") { msg = "On, but this device hasn't allowed them yet. Turn it off and on again to ask."; bad = true; }
  note.textContent = msg;
  note.classList.toggle("err", bad);
}
$("#remindNotify").onchange = async () => {
  const box = $("#remindNotify"), on = box.checked;
  if (on) {
    let perm = remindCanNotify() ? Notification.permission : "unavailable";
    if (perm === "default") { try { perm = await Notification.requestPermission(); } catch { perm = "denied"; } }
    if (perm !== "granted") {
      box.checked = false;
      return toast(perm === "unavailable" ? "This browser can't show notifications here. The note at the top still works."
        : perm === "denied" ? "Notifications are blocked for Next Box in this browser's settings." : "OK, no notifications. The note at the top still works.", 6000);
    }
  }
  try {
    me.prefs = await api("/settings", { method: "PATCH", json: { remind_notify: on } });
    remindShowPrefs(me.prefs);
    toast(on ? "Saved. You'll get a notification too, on this device." : "Saved");
  } catch (e) { box.checked = !on; toast(errText(e), { error: true }); }
};
async function remindNotify(title, body, tag) {
  if (!me?.prefs?.remind_notify || !remindCanNotify() || Notification.permission !== "granted" || document.visibilityState === "visible") return;
  const opts = { body, tag, icon: "icons/icon-192.png" };
  try {
    const n = new Notification(title, opts);
    remindShown.add(n);
    n.onclick = () => { window.focus(); n.close(); };
    n.onclose = () => remindShown.delete(n);
  } catch { try { (await navigator.serviceWorker?.getRegistration())?.showNotification(title, opts); } catch {} }  // phones: through sw.js
}
// Takes Next Box's notifications away: one reminder's (its tag), or all of them (signing out, so the next person
// on a shared device can't read the task names in the tray). Made here or through sw.js (phones).
function remindUnnotify(tag = "") {
  for (const n of remindShown) if (!tag || n.tag === tag) { remindShown.delete(n); try { n.close(); } catch {} }
  try {
    navigator.serviceWorker?.getRegistration()
      .then((r) => r?.getNotifications(tag ? { tag } : undefined))
      .then((all) => all?.forEach((n) => { if ((n.tag || "").startsWith("nb-")) n.close(); }))
      .catch(() => {});
  } catch {}
}
// Every 20 s: reminders come and go, the routine card follows the clock, and a timer that ran out long ago is
// let go. When a timed task is near and the list has been sitting 5+ minutes, it's fetched again first (in the
// background too, where notifications show), so a reminder isn't for something already done on another device.
function remindTick() {
  if (!me || $("#appShell").hidden) return;
  if (timerStale(timerState)) timerDrop();
  const visible = document.visibilityState === "visible";
  if (visible && lastLoad.day && today() !== lastLoad.day) { loadList(); loadToday(); loadRoutine(); return; }  // a new day
  const lead = me.prefs.remind_before ?? 10, now = Date.now();
  // near: from 15 minutes before a reminder starts until it ends (a snoozed one can come back at any point in that)
  const near = lead > 0 && remindTimed().some((t) => { const d = (remindAt(t) - now) / 60000; return d <= lead + 15 && d > -REMIND_LATE; });
  // Not while someone's typing or has a sheet open; in the background, only a box in the list itself (redrawn) holds it up.
  const typing = document.activeElement?.matches?.("input, textarea, select");
  const busy = visible ? $$("dialog[open]").length || typing : typing && $("#listBody").contains(document.activeElement);
  if (near && !busy && now - lastLoad.at > REMIND_STALE && now - (remindTick.asked || 0) > 60000) {
    remindTick.asked = now;  // once a minute at most, if it can't be fetched (offline)
    loadList().then(remindRender);  // drawn (and notified) from the fresh list
    return;
  }
  remindRender();
  if ($("#mylist").classList.contains("on")) routineRender();
}
setInterval(remindTick, 20000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { timerShow(); remindTick(); } });
document.addEventListener("click", (e) => { if (e.target.closest?.("#topnav button, .bottombar button, #moreMenu button, [data-go-tab]")) setTimeout(remindRender, 0); });
function remindForget() {
  remindSeen.clear(); remindNoteNow = null; remindMem = null; clearTimeout(remindNoteTimer); remindRender.mine = false;
  $("#remindList").innerHTML = ""; $("#remindList").dataset.html = ""; $("#remindBar").hidden = true; $("#remindSay").textContent = "";
  document.documentElement.style.scrollPaddingTop = ""; document.documentElement.style.removeProperty("--remindH");
  remindUnnotify();
}


// ---------------------------------------------------------------- FE: Quick add, app shortcuts, Share to Next Box
// The + at the top (or n) opens Quick add on any screen: one task or many, one per line, with Brain dump's
// rules and preview; new tasks go where Brain dump's go. manifest.json's shortcuts open #add, #snap and
// #print (the Print tab: nothing prints by itself). Sharing text to Next Box opens Quick add filled in;
// sharing a photo opens Snap with it attached (sw.js keeps it on the phone for that moment).
let quickOpener = null, quickPrevN = 0, quickPrevTimer = null, quickRoute = null;
const keysMac = () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
function quickOpen(text = null, shared = false) {
  if (!me || $("#appShell").hidden) return;
  const d = $("#quickDialog"), box = $("#quickText");
  if (text != null) box.value = text;
  $("#quickFrom").hidden = !shared;
  $("#quickMsg").textContent = ""; $("#quickMsg").className = ""; $("#quickProblems").innerHTML = "";
  $("#quickKeys").hidden = matchMedia("(pointer: coarse)").matches;  // a phone has the button right there
  $("#quickKeys kbd").textContent = keysMac() ? "⌘" : "Ctrl";
  if (!d.open) { quickOpener = document.activeElement; d.showModal(); }
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
  quickPreview();
}
$("#quickBtn").onclick = () => quickOpen();
$("#quickClose").onclick = () => $("#quickDialog").close();
closeOnBackdrop($("#quickDialog"));  // what you typed stays in the box for next time
$("#quickDialog").addEventListener("close", () => {  // back where you were (or the + when it opened by itself)
  (quickOpener?.isConnected && quickOpener !== document.body && quickOpener.offsetParent ? quickOpener : $("#quickBtn")).focus({ preventScroll: true });
  quickOpener = null;
});
$("#quickText").addEventListener("input", () => {
  clearTimeout(quickPrevTimer); quickPrevTimer = setTimeout(quickPreview, 450);
  $("#quickProblems").innerHTML = ""; $("#quickMsg").textContent = "";
});
$("#quickText").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); $("#quickGo").click(); }
});
async function quickPreview() {  // what each line becomes, as Brain dump shows it (nothing is saved)
  const text = $("#quickText").value, n = ++quickPrevN, shown = 8;
  if (!text.trim()) { $("#quickLines").innerHTML = ""; return; }
  let r;
  try { r = await api("/list/dump?preview=1", { method: "POST", json: { text } }); }
  catch { if (n === quickPrevN) $("#quickLines").innerHTML = ""; return; }  // a help, never an error
  if (n !== quickPrevN || !me) return;
  $("#quickLines").innerHTML = r.lines.slice(0, shown).map((x) => x.problem
      ? `<li class="bad"><span class="ln">${x.line}</span>${esc(x.problem)}. <span>(“${esc(x.text)}”)</span></li>`
      : x.ticked ? `<li class="left"><span class="ln">${x.line}</span>${esc(x.content)} <span class="pchip">ticked already: left out</span></li>`
      : `<li><span class="ln">${x.line}</span><b>${esc(x.content)}</b><div class="pchips">${previewChips(x, r)}</div></li>`).join("")
    + (r.lines.length > shown ? `<li class="left">…and ${plural(r.lines.length - shown, "more line")}</li>` : "");
}
$("#quickGo").onclick = () => hold($("#quickGo"), quickAdd);
async function quickAdd() {
  const text = $("#quickText").value, msg = $("#quickMsg");
  msg.className = ""; $("#quickProblems").innerHTML = "";
  if (!text.trim()) { msg.textContent = "Type something first."; return $("#quickText").focus(); }
  try {
    const r = await api("/list/dump", { method: "POST", json: { text } });
    const toApp = r.to && r.to !== "My list";
    const rest = r.skipped_text ? r.skipped_text.split("\n").filter((l) => l.trim()).length : 0;
    const said = [`Added ${r.added.length}${toApp ? ` to ${r.to}` : r.added.length ? `: ${dumpSummary(r.added)}` : ""}.`,
      rest ? `${plural(rest, "more line")} still in the box: 200 at a time.` : "",
      (r.left_out || []).length ? `Left out ${plural(r.left_out.length, "line")} ticked already.` : "",
      ...(r.notes || []), r.error || ""].filter(Boolean).join(" ");
    $("#quickText").value = r.skipped_text || "";
    $("#quickFrom").hidden = true;
    quickPreview();
    if (rest || r.error) { msg.className = r.error ? "err" : ""; msg.textContent = said; }
    else { $("#quickDialog").close(); toast(said); }
    await loadList();
    if (!toApp && $("#mylist").classList.contains("on")) flashRows(r.added.map((t) => t.id));
  } catch (e) {
    const problems = e.body?.problems || [];
    msg.className = "err";
    msg.textContent = problems.length ? `Nothing was added yet. Fix ${problems.length === 1 ? "this line" : "these lines"}, then add again:` : errText(e);
    $("#quickProblems").innerHTML = problems.map((p) => `<li><b>Line ${p.line}:</b> ${esc(p.problem)} <span class="muted">(“${esc(p.text)}”)</span></li>`).join("");
  }
}
// #add, #snap, #print (and a share's &text= or &shared=): read once, then taken off the address so a reload
// doesn't open it again. It waits for sign-in, the data notice and the setup questions.
function quickRouteRead() {
  const m = location.hash.match(/^#(add|snap|print)(?:&(text|shared)=([^&]*))?$/);
  if (!m) return;
  let v = "";
  try { v = decodeURIComponent(m[3] || ""); } catch {}
  quickRoute = { to: m[1], text: m[2] === "text" ? v.slice(0, 2000) : "", shared: m[2] === "shared" ? v : "" };
  history.replaceState(null, "", location.pathname + location.search);
}
function quickRun() {
  if (!me || $("#appShell").hidden) return;
  // Signed in, and no shared photo is waiting for this page: one left behind earlier (never picked up) goes now.
  if (!quickRoute?.shared && !quickRun.tidied) { quickRun.tidied = true; shareForget(); }
  if (!quickRoute || $$("dialog[open]").length) return;
  const r = quickRoute;
  quickRoute = null;
  if (r.to === "add") return quickOpen(r.text || null, !!r.text);
  if (r.to === "print") { goto("print"); setTimeout(() => $("[data-act=today]").focus(), 200); return; }  // never prints by itself
  goto("scan");
  if (r.shared) shareTakePhoto(r.shared);
  else setTimeout(() => $("#snapCam").focus(), 200);
}
quickRouteRead();
window.addEventListener("hashchange", () => { quickRouteRead(); quickRun(); });
$("#quizDialog").addEventListener("close", () => setTimeout(quickRun, 0));
// A photo shared to Next Box: sw.js kept it in this device's cache for a moment. It goes into Snap (it isn't
// read until you tap Read back) and leaves the cache. Older than an hour, or not there: pick it again.
const sharePhotoUrl = () => new URL("shared-photo", location.origin + location.pathname.replace(/[^/]*$/, "")).href;
async function shareTakePhoto(how) {
  let file = null;
  if (how !== "missed" && window.caches) {
    try {
      const cache = await caches.open("nb-share"), res = await cache.match(sharePhotoUrl());
      if (res) {
        await cache.delete(sharePhotoUrl());
        if (Date.now() - (+res.headers.get("X-At") || 0) < 3600000) {
          let name = "photo.jpg";
          try { name = decodeURIComponent(res.headers.get("X-Name") || name); } catch {}
          const blob = await res.blob();
          file = new File([blob], name, { type: blob.type || "image/jpeg" });
        }
      }
    } catch {}
  }
  setTimeout(() => {  // said under the buttons (and to screen readers), where a message at the bottom can't cover them
    if (file) choosePhoto(file);
    $("#scanResult").innerHTML = file ? `<p class="muted">Your shared photo is here. Tap Read back when you're ready.</p>`
      : `<p class="muted">Your photo didn't come through. Take it again, or choose it from your photos.</p>`;
    const b = file ? $("#scanGo") : $("#snapLib");
    b.scrollIntoView({ block: "nearest", behavior: smooth() });
    b.focus({ preventScroll: true });
  }, 200);
}
function shareForget() { try { window.caches?.delete("nb-share").catch(() => {}); } catch {} }
// The service worker only answers shares and notification taps (see sw.js). It needs https, or this computer.
if ("serviceWorker" in navigator && window.isSecureContext) {
  const shareWorker = () => navigator.serviceWorker.register("sw.js").catch(() => {});
  if (document.readyState === "complete") shareWorker(); else window.addEventListener("load", shareWorker);
}


// ---------------------------------------------------------------- FE: keyboard shortcuts
// n or + Quick add · / search My list · g then l, p, s or j to switch screens · ? the list of them.
// Letters do nothing while you type in a box or while a sheet is open (Esc closes a sheet), or at all with
// Settings → General → One-key shortcuts off.
const KEYS_TABS = { l: "mylist", p: "print", s: "scan", j: "journal" };
let keysG = 0;
const keysTyping = (el) => !!el?.closest?.("textarea, select, [contenteditable]:not([contenteditable=false])")
  || (el?.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(el.type));
document.addEventListener("keydown", (e) => {
  if (!me || $("#appShell").hidden || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.repeat) return;
  if (me.prefs?.keyboard_shortcuts === false) { keysG = 0; return; }  // turned off in Settings → General (W3-04)
  if (keysTyping(e.target) || $$("dialog[open]").length) { keysG = 0; return; }
  const k = e.key;
  if (keysG) {
    const tab = Date.now() - keysG < 1500 && KEYS_TABS[k.toLowerCase()];
    keysG = 0;
    if (tab) { e.preventDefault(); keysGo(tab); }
    return;
  }
  if (k === "g" || k === "G") { keysG = Date.now(); return; }
  if (k === "n" || k === "N" || k === "+") { e.preventDefault(); quickOpen(); }
  else if (k === "/") { e.preventDefault(); keysSearch(); }
  else if (k === "?") { e.preventDefault(); keysOpen(); }
});
function keysGo(tab) {
  goto(tab);
  setTimeout(() => {  // keyboard and screen readers carry on from the screen's first heading
    const h = $$(`#${tab} h2`).find((x) => x.offsetParent);
    if (h) { if (!h.hasAttribute("tabindex")) h.tabIndex = -1; h.focus({ preventScroll: true }); }
  }, 160);
}
function keysSearch() {
  if (!$("#mylist").classList.contains("on")) goto("mylist");
  if (myListView() === "schedule") { viewPick = "list"; renderList(); }  // search shows its results in List (this visit only)
  setTimeout(() => { const s = $("#listSearch"); s.scrollIntoView({ block: "center", behavior: smooth() }); s.focus({ preventScroll: true }); s.select(); }, 160);
}
function keysOpen() {
  $$("#keysDialog .modkey").forEach((k) => { k.textContent = keysMac() ? "⌘" : "Ctrl"; });
  $("#keysDialog").showModal();
}
$("#keysShow").onclick = keysOpen;
$("#keysClose").onclick = () => $("#keysDialog").close();
closeOnBackdrop($("#keysDialog"));
$("#jText").addEventListener("keydown", (e) => {  // Ctrl+Enter (⌘+Enter) saves a journal note, as it adds a Brain dump
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); $("#jSave").click(); }
});
