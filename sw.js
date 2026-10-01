// Network-first for the page files so app updates reach the phone on every
// reload; falls back to the cache when offline (the cached page can still
// control the TV over the LAN with no internet).
var CACHE = 'tvremote-v4';

self.addEventListener('install', function (e) { self.skipWaiting(); });

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  var url = new URL(e.request.url);
  var pageName = url.pathname.indexOf('socket.html') >= 0 ? 'socket.html'
    : url.pathname.indexOf('remote.html') >= 0 ? 'remote.html' : null;
  if (!pageName) return; // page files only; WebSockets bypass the SW
  e.respondWith(
    fetch(e.request).then(function (res) {
      if (res && res.ok) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
      }
      return res;
    }).catch(function () {
      // offline: exact version first, then any cached copy of the same page
      return caches.match(e.request).then(function (hit) {
        if (hit) return hit;
        return caches.open(CACHE).then(function (c) {
          return c.keys().then(function (keys) {
            for (var i = 0; i < keys.length; i++) {
              if (keys[i].url.indexOf(pageName) >= 0) return c.match(keys[i]);
            }
            throw new Error('offline');
          });
        });
      });
    })
  );
});
