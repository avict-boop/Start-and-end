"use strict";
/* Приложение на телефоне: установка на экран «Домой», служебный работник (быстрый запуск, работа без сети),
   уведомления о готовых решениях. Помощники ($, esc, api, toast) — из app.js. */

const PWA = {
  deferred: null,           // событие «можно установить» (Chrome, Android, Edge)
  reg: null,                // регистрация служебного работника
  standalone: window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true,
  ios: /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1),
};
PWA.pushable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((r) => { PWA.reg = r; pwaRender(); }).catch(() => {});
  });
}
if (PWA.standalone) document.documentElement.classList.add("standalone");

window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  PWA.deferred = e;
  pwaRender();
});
window.addEventListener("appinstalled", () => {
  PWA.deferred = null;
  try { localStorage.setItem("pwaInstalled", "1"); } catch { /* приватный режим */ }
  toast("Приложение установлено — оно на экране «Домой»");
  pwaRender();
});

async function pwaInstall() {
  if (!PWA.deferred) return;
  PWA.deferred.prompt();
  await PWA.deferred.userChoice.catch(() => null);
  PWA.deferred = null;
  pwaRender();
}

const b64ToBytes = (s) => {
  const raw = atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

async function pwaSub() {
  if (!PWA.pushable) return null;
  const reg = PWA.reg || await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function deviceLabel() {
  const ua = navigator.userAgent;
  const os = /iphone/i.test(ua) ? "iPhone" : /ipad/i.test(ua) || PWA.ios ? "iPad" : /android/i.test(ua) ? "Android"
    : /mac os/i.test(ua) ? "Mac" : /windows/i.test(ua) ? "Windows" : "устройство";
  const br = /edg\//i.test(ua) ? "Edge" : /firefox/i.test(ua) ? "Firefox" : /chrome|crios/i.test(ua) ? "Chrome" : /safari/i.test(ua) ? "Safari" : "";
  return `${os}${br ? ` · ${br}` : ""}${PWA.standalone ? " · приложение" : ""}`;
}

async function pwaPushOn() {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { toast("Уведомления запрещены — разрешите их в настройках телефона для этого сайта"); return; }
  const { key } = await api("/api/push/key");
  const reg = PWA.reg || await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  await api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label: deviceLabel() } });
  toast("Уведомления включены");
  pwaRender();
}

async function pwaPushOff() {
  const sub = await pwaSub();
  if (sub) {
    await api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
  }
  toast("Уведомления на этом устройстве выключены");
  pwaRender();
}

async function pwaPushTest() {
  const sub = await pwaSub();
  if (!sub) return;
  await api("/api/push/test", { method: "POST", body: { endpoint: sub.endpoint } });
  toast("Отправлено — уведомление придёт через пару секунд");
}

const busyBtn = (btn, fn) => async () => {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message || String(e)); } finally { btn.disabled = false; }
};

/* Блок «Приложение на телефоне» в Настройки → Программа и короткий баннер на стартовом экране */
async function pwaRender() {
  pwaBanner();
  const box = document.getElementById("pwaBox");
  if (!box) return;
  const sub = PWA.pushable ? await pwaSub().catch(() => null) : null;
  let devices = 0;
  try { devices = (await api("/api/push/key")).devices; } catch { /* нет связи */ }
  const install = PWA.standalone
    ? '<p class="ok-text small">Открыто как приложение.</p>'
    : PWA.deferred ? `<button type="button" class="btn primary" id="pwaInstallBtn"><svg class="ic" aria-hidden="true"><use href="#i-plus"/></svg>Установить приложение</button>`
    : PWA.ios ? `<ol class="pwa-steps"><li>Откройте этот сайт в <b>Safari</b>.</li>
        <li>Нажмите <b>«Поделиться»</b> <svg class="ic pwa-share" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M8 7l4-4 4 4M6 11H5v10h14V11h-1"/></svg> внизу экрана.</li>
        <li>Выберите <b>«На экран „Домой“»</b> → <b>«Добавить»</b>.</li></ol>`
    : `<p class="muted small">В Chrome на Android: меню <b>⋮</b> → <b>«Установить приложение»</b> (или «Добавить на главный экран»).
        На компьютере — значок установки в адресной строке.</p>`;
  const push = !PWA.pushable
    ? `<p class="muted small">${PWA.ios && !PWA.standalone ? "На iPhone уведомления работают только в установленном приложении — сначала добавьте его на экран «Домой» и откройте оттуда."
      : "Этот браузер не поддерживает уведомления."}</p>`
    : sub ? `<p class="ok-text small">Включены на этом устройстве${devices > 1 ? ` · всего устройств: ${devices}` : ""}.</p>
        <div class="sel-actions"><button type="button" class="btn small" id="pwaTest">Проверить</button>
        <button type="button" class="link small" id="pwaOff">Выключить на этом устройстве</button></div>`
    : `<button type="button" class="btn secondary" id="pwaOn">Включить уведомления</button>
       <p class="muted small">Придут, когда совет вынесет решение, «Отбор» будет ждать вашей проверки или заседание встанет на паузу — даже если приложение закрыто.${devices ? ` Уже включены на устройствах: ${devices}.` : ""}</p>`;
  box.innerHTML = `<h2 class="sub-h">Приложение на телефоне</h2>
    <div class="pwa-grid">
      <div><h3>Установка</h3><p class="muted small">Значок на экране, открывается без адресной строки, быстрый запуск.</p>${install}</div>
      <div><h3>Уведомления</h3>${push}</div>
    </div>
    <p class="small"><a href="/logout">Выйти на этом устройстве</a> <span class="muted">— при следующем входе спросим пароль</span></p>`;
  const on = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener("click", busyBtn(b, fn)); };
  on("pwaInstallBtn", pwaInstall);
  on("pwaOn", pwaPushOn);
  on("pwaOff", pwaPushOff);
  on("pwaTest", pwaPushTest);
}

function pwaBanner() {
  const b = document.getElementById("pwaBanner");
  if (!b) return;
  let hidden = PWA.standalone;
  try { hidden = hidden || localStorage.getItem("pwaBannerOff") === "1" || localStorage.getItem("pwaInstalled") === "1"; } catch { /* нет хранилища */ }
  const small = window.matchMedia("(max-width: 900px)").matches;
  const can = PWA.deferred || PWA.ios;
  b.hidden = hidden || !small || !can;
  if (b.hidden) return;
  b.innerHTML = `<img src="/static/icons/icon-192.png" alt="" width="40" height="40">
    <span><b>Установите приложение</b><span class="muted small">${PWA.deferred ? "значок на экране и уведомления о готовых решениях"
      : "«Поделиться» → «На экран „Домой“» — и придут уведомления"}</span></span>
    ${PWA.deferred ? '<button type="button" class="btn small primary" id="pwaBannerGo">Установить</button>' : ""}
    <button type="button" class="icon-btn" id="pwaBannerX" aria-label="Скрыть"><svg class="ic"><use href="#i-x"/></svg></button>`;
  const go = document.getElementById("pwaBannerGo");
  if (go) go.addEventListener("click", pwaInstall);
  document.getElementById("pwaBannerX").addEventListener("click", () => {
    try { localStorage.setItem("pwaBannerOff", "1"); } catch { /* нет хранилища */ }
    b.hidden = true;
  });
}

/* Уведомление из открытой вкладки — через служебного работника (на Android new Notification() не работает) */
function pwaNotify(title, body, tag, url) {
  if (!("Notification" in window) || Notification.permission !== "granted") return false;
  const opts = { body, tag, data: { url: url || location.href }, icon: "/static/icons/icon-192.png", badge: "/static/icons/maskable-192.png" };
  if (PWA.reg && PWA.reg.showNotification) { PWA.reg.showNotification(title, opts).catch(() => {}); return true; }
  try { const n = new Notification(title, opts); n.onclick = () => { window.focus(); n.close(); }; return true; } catch { return false; }
}

/* Телефон: кнопка «Начать» с оценкой закреплена внизу — под неё оставляем место по её настоящей высоте, чтобы
   низ формы (параметры, переключатели) можно было докрутить и он не прятался за кнопкой */
(function startbarSpace() {
  const bar = document.querySelector(".startbar");
  if (!bar) return;
  const set = () => {
    const fixed = getComputedStyle(bar).position === "fixed";
    document.documentElement.style.setProperty("--startbar-h", fixed ? `${Math.ceil(bar.getBoundingClientRect().height)}px` : "0px");
  };
  if ("ResizeObserver" in window) new ResizeObserver(set).observe(bar);
  window.addEventListener("resize", set);
  set();
})();

/* Отчёты на телефоне. В установленном приложении ссылка «в новой вкладке» открывается в отдельном окне браузера, у
   которого на iPhone свои cookie — вместо отчёта там страница входа. Поэтому на телефоне файл скачивается внутри
   приложения (с его входом) и отдаётся системе: «Сохранить в Файлы», открыть в Word, PDF-просмотрщике, переслать. */
function pwaShouldIntercept(a) {
  if (!a || !a.href) return false;
  const u = new URL(a.href, location.href);
  if (u.origin !== location.origin || !/^\/api\/sessions\/[^/]+\/(report|selection\.xlsx|files\/)/.test(u.pathname)) return false;
  if (u.searchParams.get("format") === "html") return PWA.standalone;  // HTML-отчёт: в приложении — в том же окне
  return PWA.standalone || PWA.ios || /android/i.test(navigator.userAgent);
}

function fileNameFrom(resp, fallback) {
  const cd = resp.headers.get("content-disposition") || "";
  const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) { try { return decodeURIComponent(star[1]); } catch { /* испорченное имя */ } }
  const plain = cd.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

async function pwaDownload(a) {
  const u = new URL(a.href, location.href);
  if (u.searchParams.get("format") === "html") { location.href = u.href; return; }
  toast("Готовлю файл…");
  const resp = await fetch(u.href, { credentials: "same-origin" });
  if (!resp.ok) { toast(resp.status === 401 || resp.redirected ? "Войдите заново и повторите" : `Не скачалось: HTTP ${resp.status}`); return; }
  const blob = await resp.blob();
  const name = fileNameFrom(resp, "report");
  const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try { await navigator.share({ files: [file], title: name }); return; }
    catch (e) { if (e && e.name === "AbortError") return; }  // закрыл меню — ничего не делаем
  }
  const url = URL.createObjectURL(blob);  // нет «Поделиться» — обычное сохранение файла
  const tmp = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(tmp);
  tmp.click();
  tmp.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("a[href]");
  if (!pwaShouldIntercept(a) || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey) return;
  e.preventDefault();
  if (typeof closeMenus === "function") closeMenus();
  pwaDownload(a).catch((err) => toast(`Не скачалось: ${err.message || err}`));
}, true);

/* Нижняя панель разделов на телефоне. В разметке она внутри шапки (на компьютере это боковая колонка), а шапка на
   телефоне закреплена сверху (sticky). Safari на iPhone, особенно в приложении с экрана «Домой», иногда перестаёт
   рисовать закреплённый снизу элемент внутри закреплённого сверху — панель пропадала. На телефоне переносим её прямо
   в <body>, на широком экране возвращаем в колонку. Обработчики нажатий при переносе сохраняются. */
(function navOutOfRail() {
  const nav = document.querySelector(".rail .nav");
  const rail = document.querySelector(".rail");
  if (!nav || !rail) return;
  const mq = window.matchMedia("(max-width: 899px)");
  const place = () => {
    if (mq.matches) { if (nav.parentElement !== document.body) document.body.appendChild(nav); }
    else if (nav.parentElement !== rail) rail.insertBefore(nav, rail.querySelector(".recent") || null);
  };
  place();
  if (mq.addEventListener) mq.addEventListener("change", place); else mq.addListener(place);
  // после клавиатуры и возврата в приложение iOS бывает «теряет» нижнюю панель — перерисовываем
  const nudge = () => { if (!mq.matches) return; nav.style.display = "none"; void nav.offsetHeight; nav.style.display = ""; };
  if (window.visualViewport) window.visualViewport.addEventListener("resize", nudge);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) nudge(); });
  window.addEventListener("pageshow", nudge);
})();

