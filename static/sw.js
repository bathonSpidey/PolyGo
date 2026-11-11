/* PolyGo's service worker: it only shows a notification when the server sends a push (an invitation to talk).
   It caches nothing and answers no page requests, so it can never show an old copy of the page. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) {}
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = wins.filter(c => c.visibilityState === "visible");
    if (open.length) { open.forEach(c => c.postMessage({ type: "invites" })); return; }   // PolyGo is on screen: it shows the invitation itself
    await self.registration.showNotification(d.title || "PolyGo", {
      body: d.body || "", tag: d.tag || "polygo", renotify: true, icon: "/icon-192.png", badge: "/icon-192.png",
      vibrate: [250, 120, 250, 120, 250], requireInteraction: true, data: { url: d.url || "/#invited" }
    });
  })());
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/#invited";
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of wins) {
      if ("focus" in c) { await c.focus(); c.postMessage({ type: "invites", asked: true }); return; }
    }
    await self.clients.openWindow(url);
  })());
});
