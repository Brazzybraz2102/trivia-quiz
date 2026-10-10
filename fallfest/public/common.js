// Shared helpers for the host screen and the player phones.
"use strict";

window.FF = (() => {
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
      else if (k === "value") n.value = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat(Infinity)) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }

  // Live state over Server-Sent Events. Reconnects on its own.
  let offset = 0;
  function connect(url, onState, onStatus) {
    let es;
    const open = () => {
      es = new EventSource(url);
      es.onopen = () => onStatus && onStatus(true);
      es.onerror = () => onStatus && onStatus(false);
      es.onmessage = e => {
        const s = JSON.parse(e.data);
        offset = s.now - Date.now();
        onState(s);
      };
    };
    open();
    return { close: () => es && es.close() };
  }
  const serverNow = () => Date.now() + offset;

  // Any element with data-deadline shows the seconds left.
  setInterval(() => {
    for (const el of document.querySelectorAll("[data-deadline]")) {
      const left = Math.max(0, Math.ceil((+el.dataset.deadline - serverNow()) / 1000));
      if (el.textContent !== String(left)) {
        el.textContent = left;
        el.classList.toggle("urgent", left <= 5);
        el.dispatchEvent(new CustomEvent("tick", { detail: left }));
      }
    }
  }, 200);
  const countdown = deadline => deadline ? h("div", { class: "clock", "data-deadline": deadline, role: "timer", "aria-label": "Seconds left" }, "") : null;

  async function post(path, body) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      return await r.json();
    } catch { return { error: "Can't reach the game. Check that you're on the same Wi-Fi." }; }
  }

  const avatar = (p, size) => h("span", { class: "avatar" + (size ? " " + size : ""), style: { background: p.color }, "aria-hidden": "true" }, p.emoji);
  const byId = players => Object.fromEntries(players.map(p => [p.id, p]));
  const pts = n => (n > 0 ? "+" : "") + n.toLocaleString("en-US");
  // Unitless guess answers are years, which read wrong with a thousands comma.
  const num = (n, unit) => unit ? (+n).toLocaleString("en-US") : String(n);

  return { h, connect, serverNow, countdown, post, avatar, byId, pts, num };
})();
