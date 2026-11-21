/* Служебный работник приложения «Совет нейросетей»: быстрый запуск и работа без сети (оболочка из кэша),
   уведомления о готовых решениях (Web Push) и переход к заседанию по нажатию на уведомление.
   Сервер подставляет версию и список файлов оболочки; данные (/api) никогда не кэшируются. */
"use strict";
const VERSION = "__VERSION__";
const CACHE = `council-${VERSION}`;
const SHELL = "__SHELL__";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("council-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

const OFFLINE = `<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Нет связи — Совет нейросетей</title><body style="font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;padding:16px;text-align:center;color:#18233d;background:#f7f8fb">
<div><h1 style="font-size:1.3rem">Нет связи с сервером</h1><p>Проверьте интернет. Заседания на сервере продолжаются — результат не потеряется.</p>
<button onclick="location.reload()" style="font:inherit;padding:.7rem 1.4rem;border:0;border-radius:12px;background:#4f46e5;color:#fff">Повторить</button></div>`;

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/login" || url.pathname === "/logout") return;
  if (req.mode === "navigate") {
    // страница — всегда свежая с сервера; без сети — сохранённая оболочка или короткое объяснение
    e.respondWith(fetch(req).then((r) => {
      // сохраняем только саму программу — не страницу входа, на которую сервер перенаправил бы после выхода
      if (r.ok && !r.redirected && url.pathname === "/") { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/").then((r) => r || new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } }))));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    // файлы оформления и скрипты — из кэша (у них версия в адресе), в фоне — обновление
    e.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => {
      const net = fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; });
      if (hit) { net.catch(() => {}); return hit; }  // без сети — из кэша, фоновое обновление молча не удалось
      return net;
    })));
  }
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Совет нейросетей", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || "/" },
    icon: "/static/icons/icon-192.png", badge: "/static/icons/maskable-192.png", lang: "ru",
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || "/", location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (new URL(c.url).origin === location.origin) return c.focus().then(() => c.navigate ? c.navigate(target) : c);
    }
    return self.clients.openWindow(target);
  }));
});

self.addEventListener("message", (e) => { if (e.data === "skipWaiting") self.skipWaiting(); });
