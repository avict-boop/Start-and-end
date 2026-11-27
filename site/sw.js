// Кэш оболочки приложения: страница открывается без сети, справочники обновляются при наличии сети.
const CACHE = "bad-checker-v1";
const SHELL = ["./", "index.html", "css/app.css", "js/engine.js", "js/app.js", "manifest.webmanifest",
  "icons/icon-192.png", "data/prohibited.json", "data/nutrients.json", "data/additives.json", "data/rules.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return; // CDN распознавания кэширует сам себя
  // Сначала сеть (свежие справочники и код), при отсутствии сети — кэш
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match(e.request)));
});
