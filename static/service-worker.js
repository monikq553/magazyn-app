const CACHE_NAME = "pmagazyn-shell-v3";
const SHELL_ASSETS = [
  "/static/favicon.ico",
  "/static/icons/icon-180.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

const BYPASS_PREFIXES = [
  "/auth/",
  "/api/",
  "/logout",
  "/import_excel",
  "/import-ogolny",
  "/import-wydan",
  "/admin/backups",
  "/service-worker.js",
  "/manifest.json"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    BYPASS_PREFIXES.some(prefix => url.pathname.startsWith(prefix))
  ) {
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;
        return fetch(request).then(response => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
          return response;
        });
      })
    );
  }
});

self.addEventListener("push", event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = { title: "Pmagazyn", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(payload.title || "Pmagazyn", {
      body: payload.body || "Nowa rezerwacja do przygotowania",
      data: payload.url || "/rezerwacje"
    })
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = event.notification.data || "/rezerwacje";
  event.waitUntil(clients.openWindow(url));
});
