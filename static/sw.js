// VideoWatch Service Worker — Web Push + PWA offline shell
const CACHE = 'vw-shell-v1';
const SHELL = [
  '/',
  '/static/index.html',
  '/static/og-image.svg',
  '/static/manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only cache GET requests for same-origin pages/assets; let API calls pass through
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        // Cache fresh HTML/JS/CSS/SVG responses
        if (res.ok && ['/', '/static/index.html', '/static/og-image.svg'].includes(url.pathname)) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});

// ── Web Push ─────────────────────────────────────────────────────────────────

self.addEventListener('push', function(event) {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); } catch(e) { data = { title: 'VideoWatch', body: event.data.text() }; }
  event.waitUntil(
    self.registration.showNotification(data.title || 'VideoWatch', {
      body: data.body || 'New content detected.',
      icon: data.icon || '/static/og-image.svg',
      badge: '/static/og-image.svg',
      data: { url: data.url || '/' },
      tag: data.tag || 'vw-notification',
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const client of list) {
      if (client.url.includes(self.location.origin) && 'focus' in client) {
        client.navigate(url);
        return client.focus();
      }
    }
    return clients.openWindow(url);
  }));
});
