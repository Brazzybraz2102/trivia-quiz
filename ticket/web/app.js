const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function getKey() { try { return localStorage.getItem("ticketKey") || ""; } catch { return ""; } }
function setKey(k) { try { localStorage.setItem("ticketKey", k); } catch {} }

async function api(path, opts = {}) {
  const headers = { "X-Ticket-Key": getKey(), ...(opts.headers || {}) };
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status}`);
  return body;
}
const png = (id) => `/printed/${id}/png?k=${encodeURIComponent(getKey())}&t=${Date.now()}`;

// tabs
document.querySelectorAll("nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll("section").forEach((s) => s.classList.toggle("on", s.id === b.dataset.tab));
  if (b.dataset.tab === "printed") loadHistory();
});

async function refreshStatus() {
  if (!getKey()) { $("#status").textContent = "Set your key in Settings"; return; }
  try {
    const s = await api("/status");
    $("#status").textContent = `printer ${s.printer_reachable ? "online" : "offline"}${s.dry_run_forced ? " · server dry-run" : ""}`;
  } catch (e) { $("#status").textContent = `server: ${e.message}`; }
}

function showPrint(r) {
  const box = $("#printResult");
  box.hidden = false;
  if (r.status === "skipped") { box.innerHTML = `<b>Skipped</b> ${esc(r.reason)}`; return; }
  box.innerHTML = `<b>${r.status === "printed" ? "Printed" : "Dry run"}</b> #${esc(r.id)} · ${r.manifest.length} rows
    <img src="${png(r.id)}" alt="label preview">`;
}

document.querySelectorAll("[data-act]").forEach((b) => b.onclick = async () => {
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

const VERB = { drop: "Delete", done: "Complete", tomorrow: "Move to tomorrow" };

function renderScan(rec) {
  const pend = rec.needs_confirmation.filter((p) => p.status === "pending");
  let h = `<div class="card"><h2>Scan ${esc(rec.id)}${rec.label_id ? ` · label #${esc(rec.label_id)}` : ""}</h2>`;
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

async function loadHistory() {
  try {
    const items = await api("/printed?limit=30");
    $("#hist").innerHTML = items.length ? items.map((r) => `<div class="card">
      <div class="item"><b>${esc(r.title).slice(0, 50)}</b><span class="muted">#${esc(r.id)}</span></div>
      <div class="muted">${esc(r.created_at.replace("T", " "))} · ${esc(r.kind)} · ${esc(r.source)}${r.dry_run ? " · dry run" : ""}</div>
      <img loading="lazy" src="${png(r.id)}" alt=""></div>`).join("") : `<p class="muted">Nothing printed yet.</p>`;
  } catch (e) { $("#hist").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

$("#key").value = getKey();
$("#saveKey").onclick = () => { setKey($("#key").value.trim()); refreshStatus(); };
refreshStatus();
