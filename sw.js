// Serves the cached remote.html when a-Shell (and its file server) is asleep.
// Each ?v=N version is cached under its own URL, so updates still work:
// just open the new versioned URL once while the relay is awake.
var CACHE = 'tvremote-v1';

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
  if (url.pathname.indexOf('remote.html') < 0) return; // page only; WS bypasses SW
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      if (hit) return hit; // cache-first: instant even when a-Shell is asleep
      return fetch(e.request).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        return res;
      }).catch(function () {
        // offline and this exact version was never cached: serve any cached copy
        return caches.open(CACHE).then(function (c) {
          return c.keys().then(function (keys) {
            for (var i = 0; i < keys.length; i++) {
              if (keys[i].url.indexOf('remote.html') >= 0) return c.match(keys[i]);
            }
            throw new Error('offline');
          });
        });
      });
    })
  );
});
