// VideoWatch Service Worker — handles Web Push notifications
self.addEventListener('push', function(event) {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); } catch(e) { data = { title: 'VideoWatch', body: event.data.text() }; }
  event.waitUntil(
    self.registration.showNotification(data.title || 'VideoWatch', {
      body: data.body || 'New content detected.',
      icon: data.icon || '/static/og-image.png',
      badge: '/static/og-image.png',
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
