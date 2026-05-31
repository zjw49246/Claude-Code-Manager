const CACHE_NAME = 'cc-manager-v2';
const STATIC_ASSETS = ['/icons/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Network-first for API and WebSocket — let them fall through to the network.
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) {
    return;
  }

  // Network-first for navigation requests and index.html so new deploys are picked up immediately.
  // Vite-built JS/CSS in /assets have content-hashed filenames, so a fresh index.html guarantees fresh assets.
  const isNavigation = event.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html');
  if (isNavigation) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match(event.request).then((cached) => cached || Response.error()))
    );
    return;
  }

  // Cache-first for hashed static assets (safe — filename changes when content changes).
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
