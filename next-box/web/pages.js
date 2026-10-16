// Loaded in <head>: the Theme, Text size and Easy-to-read text picked in the app (Settings → General) apply
// here too, before anything shows. The app keeps a copy of them on this device (app.js applyLook).
try { const t = localStorage.getItem("theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch {}
try { const z = localStorage.getItem("nb.textSize"); if (z === "large" || z === "xlarge") document.documentElement.dataset.text = z; } catch {}
try { document.documentElement.toggleAttribute("data-reading", localStorage.getItem("nb.reading") === "1"); } catch {}

// Fills in who runs this Next Box, how to reach them and what it runs on, from /about.
// An element with data-needs="x" shows only when /about has a value for x.
function fillFacts() {
  fetch("/about").then((r) => r.json()).then((a) => {
    const fill = (sel, value) => { if (value) document.querySelectorAll(sel).forEach((el) => { el.textContent = value; }); };
    fill("[data-operator]", a.operator);
    fill("[data-version]", a.version);
    fill("[data-retention]", a.retention_days);
    fill("[data-db]", a.database);
    fill("[data-hosting]", a.hosting);
    document.querySelectorAll("[data-needs]").forEach((el) => { el.hidden = !a[el.dataset.needs]; });
    document.querySelectorAll("[data-contact]").forEach((el) => {
      if (a.contact) { el.innerHTML = ""; const link = document.createElement("a"); link.href = "mailto:" + a.contact; link.textContent = a.contact; el.appendChild(link); }
    });
  }).catch(() => {});
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fillFacts); else fillFacts();
