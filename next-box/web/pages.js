// Fills in who runs this Next Box and how to reach them, from /about.
fetch("/about").then((r) => r.json()).then((a) => {
  const fill = (sel, value) => { if (value) document.querySelectorAll(sel).forEach((el) => { el.textContent = value; }); };
  fill("[data-operator]", a.operator);
  fill("[data-version]", a.version);
  fill("[data-retention]", a.retention_days);
  document.querySelectorAll("[data-contact]").forEach((el) => {
    if (a.contact) { el.innerHTML = ""; const link = document.createElement("a"); link.href = "mailto:" + a.contact; link.textContent = a.contact; el.appendChild(link); }
  });
}).catch(() => {});
