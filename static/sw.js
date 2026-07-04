// VideoWatch Service Worker — Web Push + PWA offline shell
const SHELL_CACHE  = 'vw-shell-v2';
const API_CACHE    = 'vw-api-v1';
const THUMB_CACHE  = 'vw-thumbs-v1';
const THUMB_MAX    = 300;

const SHELL = [
  '/',
  '/static/index.html',
  '/static/og-image.svg',
  '/static/manifest.json',
];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(SHELL_CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => ![SHELL_CACHE, API_CACHE, THUMB_CACHE].includes(k)).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);

  // Thumbnails — cache-first, network fallback, max THUMB_MAX entries
  if (url.pathname.match(/\.(jpe?g|png|webp|gif|svg)$/i) || url.pathname.startsWith('/thumbs/')) {
    e.respondWith(thumbStrategy(e.request));
    return;
  }

  // API video/site list — network-first, stale fallback
  if (url.pathname.startsWith('/api/videos') || url.pathname.startsWith('/api/sites')) {
    e.respondWith(networkFirstApi(e.request));
    return;
  }

  // Shell & static assets — network-first, cache fallback
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok && ['/','/ static/index.html','/static/og-image.svg'].includes(url.pathname)) {
          caches.open(SHELL_CACHE).then(c => c.put(e.request, res.clone()));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});

async function networkFirstApi(request) {
  try {
    const res = await fetch(request);
    if (res.ok) {
      const cache = await caches.open(API_CACHE);
      cache.put(request, res.clone());
    }
    return res;
  } catch {
    const cached = await caches.match(request);
    return cached || new Response(JSON.stringify([]), { headers: { 'Content-Type': 'application/json' } });
  }
}

async function thumbStrategy(request) {
  const cache = await caches.open(THUMB_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    if (res.ok) {
      // Evict oldest if over limit
      const keys = await cache.keys();
      if (keys.length >= THUMB_MAX) await cache.delete(keys[0]);
      cache.put(request, res.clone());
    }
    return res;
  } catch {
    return new Response('', { status: 404 });
  }
}

// ── Web Push ──────────────────────────────────────────────────────────────────
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
