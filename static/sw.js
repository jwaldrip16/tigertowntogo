// Fleet Foot Delivery service worker: shell cache so the app opens offline, live data always from network.
const SHELL = 'fleetfoot-shell-v3';
const FILES = ['/static/style.css'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;                              // maps, scripts from other sites: browser handles
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;   // orders are never cached
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
