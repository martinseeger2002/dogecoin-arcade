// DogecoinArcade's service worker (revision {{ revision }}).
//
// Two jobs and no more. It keeps ONE page, /offline, for when the node cannot be
// reached. And it shows a notification when a push arrives for the Messenger.
// It never caches a page, a balance, a key or a message: those always come live
// from the node, so an update is never hidden behind an old copy and nothing
// private is written to the phone by this file. A new revision replaces it.
const CACHE = 'arcade-{{ revision }}';
const KEEP = ['/offline', '/icon-192.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(KEEP)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(names => Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

// Pages always from the network; the offline page only when there is none.
self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match('/offline')));
});

// A push carries nothing (arcade/push.py): it only wakes this worker, which asks
// the node -- with this person's own session -- who wrote. Never what they wrote.
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let news = [];
    try {
      const r = await fetch('/account/push/news', {credentials: 'include', cache: 'no-store'});
      if (r.ok) news = (await r.json()).news || [];
    } catch (e) { /* the node is away: say something anyway */ }
    const fills = news.filter(n => n.kind === 'fill');
    if (fills.length) {
      return self.registration.showNotification('Your buy order can fill', {
        body: `A sell order meets your price for ${fills[0].name}. Open the arcade to complete it.`,
        tag: 'arcade-fills', renotify: true, icon: '/icon-192.png', badge: '/icon-192.png',
        data: {url: fills[0].url || '/exchange?tab=tokens'}});
    }
    news = news.filter(n => n.kind !== 'fill');
    const who = [...new Set(news.map(n => n.from))];
    const title = who.length === 1 ? 'Message from ' + who[0]
                : who.length > 1 ? who.length + ' people wrote to you'
                : 'New message';
    const body = news.length > 1 ? news.length + ' new messages' : 'Open the arcade to read it.';
    // Every push must show something (a phone revokes a site that pushes
    // silently), and one tag means a burst of messages is one notification.
    return self.registration.showNotification(title, {
      body, tag: 'arcade-messages', renotify: true,
      icon: '/icon-192.png', badge: '/icon-192.png', data: {url: '/me/messages'}});
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of open) {
      if ('focus' in c) { await c.focus(); if ('navigate' in c) c.navigate(url); return; }
    }
    return self.clients.openWindow(url);
  })());
});
