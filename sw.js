// Serves the cached page (remote.html + socket.html) when a-Shell (and its
// file server) is asleep. Each ?v=N version is cached under its own URL, so
// updates still work: just open the new versioned URL once while online.
var CACHE = 'tvremote-v2';

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
  var isPage = url.pathname.indexOf('remote.html') >= 0 || url.pathname.indexOf('socket.html') >= 0;
  if (!isPage) return; // page files only; WebSockets bypass the SW
  var pageName = url.pathname.indexOf('socket.html') >= 0 ? 'socket.html' : 'remote.html';
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
              if (keys[i].url.indexOf(pageName) >= 0) return c.match(keys[i]);
            }
            throw new Error('offline');
          });
        });
      });
    })
  );
});
