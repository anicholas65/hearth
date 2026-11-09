/* Hearth service worker: app shell cache + recently viewed pages for offline reading.
   Writes are never cached; the shopping list queues them itself (offline.js). */
const VERSION = 'hearth-__VERSION__';
const SHELL = ['/static/style.css?v=__VERSION__', '/static/offline.html', '/static/offline.js?v=__VERSION__',
  '/static/brand/hearth-icon.svg', '/static/brand/icon-192.png', '/static/favicon.svg', '/static/manifest.webmanifest'];
const PAGES = VERSION + '-pages', MAX_PAGES = 40;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(k => !k.startsWith(VERSION)).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

async function trim(cache) {
  const keys = await cache.keys();
  for (const k of keys.slice(0, Math.max(0, keys.length - MAX_PAGES))) await cache.delete(k);
}

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/static/') || url.pathname.match(/^\/photos\/\d+\/thumb$/)) {
    // cache-first for static assets and thumbnails
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok) caches.open(VERSION).then(c => c.put(req, res.clone()));
      return res;
    })));
    return;
  }
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    // network-first; keep a copy so recently viewed pages open offline
    e.respondWith(fetch(req).then(res => {
      if (res.ok && !url.searchParams.has('msg')) {
        const copy = res.clone();
        caches.open(PAGES).then(c => c.put(req, copy).then(() => trim(c)));
      }
      return res;
    }).catch(async () => (await caches.match(req, {ignoreSearch: false})) ||
                         (await caches.match(url.pathname, {ignoreSearch: true})) ||
                         caches.match('/static/offline.html')));
  }
});
