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
// Web push: real notifications while the app is closed.
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data.json(); } catch (err) { d = {title: 'Fleet Foot Delivery', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Fleet Foot Delivery', {
    body: d.body || '',
    tag: d.tag || 'ff',
    icon: d.icon || '/brand/icon-192.png',
    badge: d.icon || '/brand/icon-192.png',
    renotify: true,
    data: {url: d.url || '/'}
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (let i = 0; i < list.length; i++){ const c = list[i]; if ('focus' in c) return c.focus(); }
    return clients.openWindow(url);
  }));
});
