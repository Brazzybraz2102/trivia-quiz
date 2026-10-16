// Next Box's service worker does two small things, and nothing else:
//  1. Share to Next Box (manifest.json share_target): a shared photo is kept on this device for a moment and
//     opens Snap with it attached; shared text opens Quick add filled in. Nothing is sent anywhere.
//  2. A tap on a reminder notification brings Next Box to the front.
// It doesn't cache the app or answer any other request (those go to the network as always), never shows a
// notification itself, and there's no push: reminders are worked out by the open page.
const SCOPE = self.registration.scope;              // …/app/
const SHARE = new URL("share", SCOPE).pathname;      // /app/share
const SHARE_CACHE = "nb-share";
const SHARE_TEXT_MAX = 2000;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(Promise.all([self.clients.claim(), shareTidy()])));

// A shared photo that was never picked up doesn't stay on the device: after an hour it's let go (the app
// also lets it go once someone signs in without one waiting, and on sign-out).
async function shareTidy() {
  try {
    if (!(await caches.has(SHARE_CACHE))) return;
    const res = await (await caches.open(SHARE_CACHE)).match(new URL("shared-photo", SCOPE).href);
    if (!res || Date.now() - (+res.headers.get("X-At") || 0) > 3600000) await caches.delete(SHARE_CACHE);
  } catch {}
}

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "POST" || new URL(e.request.url).pathname !== SHARE) return;
  e.respondWith(shareIn(e.request));
});

// The title, text and link once each (a phone often puts the link in the text too). A shared list keeps
// its lines without the title. The same rule as server.share_text, which answers when this isn't running.
function shareText(title, text, url) {
  const str = (v) => (typeof v === "string" ? v.trim() : "");
  const parts = [str(title), str(text), str(url)].filter(Boolean), low = parts.map((p) => p.toLowerCase());  // "Dentist" is in "Call the dentist" too
  let keep = parts.filter((p, i) => low.indexOf(low[i]) === i && !low.some((q, j) => j !== i && low[i] !== q && q.includes(low[i])));
  const lines = str(text).includes("\n");
  if (lines && str(title) && str(title) !== str(text)) keep = keep.filter((p) => p !== str(title));
  return keep.join(lines ? "\n" : " ").slice(0, SHARE_TEXT_MAX);
}

async function shareIn(request) {
  const go = (hash) => Response.redirect(new URL("./" + hash, SCOPE).href, 303);
  let form;
  try { form = await request.formData(); } catch { return go("#add"); }
  const photo = form.getAll("photo").find((f) => f && typeof f !== "string" && f.size > 0);
  if (photo) {
    try {
      const cache = await caches.open(SHARE_CACHE);
      await cache.put(new URL("shared-photo", SCOPE).href, new Response(photo, { headers: {
        "Content-Type": photo.type || "image/jpeg", "X-Name": encodeURIComponent(photo.name || "photo.jpg"), "X-At": String(Date.now()) } }));
      return go("#snap&shared=1");
    } catch { return go("#snap&shared=missed"); }
  }
  const text = shareText(form.get("title"), form.get("text"), form.get("url"));
  return go("#add" + (text ? "&text=" + encodeURIComponent(text) : ""));
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    const open = list.find((c) => c.url.startsWith(SCOPE));
    return open ? open.focus() : self.clients.openWindow(SCOPE);
  }));
});
