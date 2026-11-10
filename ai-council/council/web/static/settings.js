/* Страница «Настройки»: участники и модели, параметры заседания, проверка фактов, оформление, версия программы.
   Использует общие функции из app.js: $, esc, api, showView, closeStream, markActiveHistory, initForm. */

const SET = { data: null, orig: "", tab: "members", writable: true, logo: "", models: null, upgrades: null,
  sys: null, check: null, poll: null, restarting: false };
const EFFORTS = [["", "как в общих"], ["none", "не задавать"], ["low", "низкая"], ["medium", "средняя"], ["high", "высокая"]];
const MODES = [["debate", "Дебаты"], ["quick", "Быстрый"]];

function setDirty() {
  const dirty = JSON.stringify(SET.data) !== SET.orig;
  $("setSave").disabled = !dirty || !SET.writable;
  $("setState").textContent = dirty ? "Есть несохранённые изменения" : "";
  $("saveBar").classList.toggle("clean", !dirty);
  return dirty;
}

function notice(html, kind = "") {
  $("setNotice").innerHTML = html ? `<div class="set-notice ${kind}">${html}</div>` : "";
}

async function openSettings() {
  if (typeof closeStream === "function") closeStream();
  current = null;
  history.replaceState(null, "", "#/settings");
  showView("settings");
  markActiveHistory();
  notice("");
  try {
    const r = await api("/api/settings");
    SET.data = r.settings; SET.orig = JSON.stringify(r.settings); SET.writable = r.writable; SET.logo = r.logo;
    SET.logoWritable = r.logo_writable;
  } catch (e) {
    $("setBody").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }
  if (!SET.writable) {
    notice("Настройки сейчас только для просмотра: у веб-интерфейса нет прав на запись config.yaml. "
      + "На сервере один раз выполните:<br><code>sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install</code>", "warn");
  }
  loadModels();
  renderSettings();
  setDirty();
}
window.openSettings = openSettings;

async function loadModels() {
  if (SET.models) return;
  try {
    const r = await api("/api/models?limit=2000");
    SET.models = r.available ? r.items : [];
    $("modelList").innerHTML = SET.models.map((m) => `<option value="${esc(m.id)}">${esc(m.date)} · $${m.prompt}/$${m.completion}</option>`).join("");
    if (SET.tab === "members") renderSettings();
  } catch { SET.models = []; }
}

function modelInfo(id) {
  if (!SET.models || !SET.models.length) return "";
  const m = SET.models.find((x) => x.id === id);
  if (!m) return `<span class="bad-text">нет на OpenRouter</span>`;
  return `${esc(m.date)} · $${m.prompt} / $${m.completion} за 1M${m.image ? " · 🖼" : ""}${m.audio ? " · 🎤" : ""}`;
}

function sel(id, options, value, attrs = "") {
  return `<select id="${id}" ${attrs}>${options.map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(value ?? "") ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
}

function renderSettings() {
  for (const b of $("setTabs").querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.tab === SET.tab);
    b.setAttribute("aria-selected", String(b.dataset.tab === SET.tab));
  }
  const d = SET.data;
  if (!d) return;
  const ro = SET.writable ? "" : "disabled";
  let html = "";
  if (SET.tab === "members") {
    const enabled = d.members.filter((m) => m.enabled).map((m) => [m.name, m.name]);
    html = `
      <p class="muted small">Модель — id на OpenRouter (начните вводить, появятся подсказки: дата выхода и цена за 1M токенов вход/выход).
        Изменения действуют со следующего заседания.</p>
      <div class="table-wrap"><table class="table set-members">
        <thead><tr><th>Вкл.</th><th>Имя</th><th>Модель</th><th>Глубина рассуждений</th><th><span class="sr-only">Удалить</span></th></tr></thead>
        <tbody>${d.members.map((m, i) => `<tr class="${m.enabled ? "" : "off"}">
          <td class="c-on"><label class="switch small" title="Участвует в заседаниях"><input type="checkbox" data-mi="${i}" data-f="enabled" ${m.enabled ? "checked" : ""} ${ro} aria-label="${esc(m.name)} участвует"><span class="sw" aria-hidden="true"></span></label></td>
          <td class="c-name" data-th="Имя"><input type="text" data-mi="${i}" data-f="name" value="${esc(m.name)}" ${m._new ? "" : "readonly"} ${ro} class="w-name" aria-label="Имя участника"></td>
          <td data-th="Модель на OpenRouter"><input type="text" data-mi="${i}" data-f="model" value="${esc(m.model)}" list="modelList" ${ro} class="w-model" aria-label="Модель ${esc(m.name)}">
            <div class="muted small" data-info="${i}">${modelInfo(m.model)}</div></td>
          <td data-th="Глубина рассуждений">${sel(`eff${i}`, EFFORTS, m.reasoning_effort || "", `data-mi="${i}" data-f="reasoning_effort" ${ro} aria-label="Глубина рассуждений ${esc(m.name)}"`)}</td>
          <td class="c-del"><button type="button" class="icon-btn" data-del="${i}" ${ro} title="Удалить участника" aria-label="Удалить участника ${esc(m.name)}"><svg class="ic"><use href="#i-trash"/></svg></button></td></tr>`).join("")}</tbody>
      </table></div>
      <button type="button" class="secondary" id="addMember" ${ro} style="margin-top:.4rem"><svg class="ic" aria-hidden="true"><use href="#i-plus"/></svg>Добавить участника</button>
      <div class="row2" style="margin-top:1rem">
        <label class="field"><span>Председатель</span>${sel("sChair", enabled, d.chair, ro)}</label>
        <label class="field"><span>Адвокат дьявола</span>${sel("sDevil", [["", "— нет —"], ...enabled], d.devil_advocate || "", ro)}</label>
      </div>
      <label class="switch"><input type="checkbox" id="sChairProposes" ${d.chair_proposes ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span>
        <span><b>Председатель тоже предлагает решение</b><small>По умолчанию он только ставит задачу, голосует и пишет итог.</small></span></label>
      <label class="switch"><input type="checkbox" id="sChairFollows" ${d.chair_follows_vote ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span>
        <span><b>Председатель следует голосованию</b><small>Другой вариант — только при опровергнутых фактах, серьёзном риске или если лидер не отвечает на вопрос.</small></span></label>
      <h2 style="margin-top:1.4rem">Более новые модели</h2>
      <p class="muted small">Сравнение моделей совета с каталогом OpenRouter: что вышло новее того же семейства и у того же разработчика.</p>
      <button type="button" class="secondary" id="checkUpgrades">Проверить, есть ли модели новее</button>
      <div id="upgrades"></div>`;
  } else if (SET.tab === "session") {
    html = `
      <p class="muted small">Значения по умолчанию для новых заседаний (в форме вопроса их можно поменять для отдельного заседания).</p>
      <div class="row2">
        <label class="field"><span>Режим по умолчанию</span>${sel("sMode", MODES, d.mode, ro)}</label>
        <label class="field"><span>Раундов «критика → доработка»</span><input type="number" id="sRounds" min="0" max="5" value="${d.debate_rounds}" ${ro}></label>
        <label class="field"><span>Лимит стоимости заседания, $</span><input type="number" id="sBudget" min="0.01" step="any" value="${d.max_cost_usd}" ${ro}></label>
        <label class="field"><span>Кворум (минимум вариантов)</span><input type="number" id="sQuorum" min="1" value="${d.min_quorum}" ${ro}></label>
        <label class="field"><span>Глубина рассуждений «думающих» моделей</span>${sel("sEffort", EFFORTS.slice(1), d.reasoning_effort, ro)}</label>
        <label class="field"><span>Лимит длины ответа, токенов <em>(длинные ответы продолжаются автоматически)</em></span><input type="number" id="sMaxTokens" min="500" step="500" value="${d.max_tokens}" ${ro}></label>
        <label class="field"><span>Температура <em>(0 — строже, 1 — креативнее)</em></span><input type="number" id="sTemp" min="0" max="2" step="0.1" value="${d.temperature}" ${ro}></label>
        <label class="field"><span>Язык ответов <em>(по-английски: Russian, English…)</em></span><input type="text" id="sLang" value="${esc(d.language)}" ${ro}></label>
      </div>
      <h2 style="margin-top:1rem">Экономия</h2>
      <label class="switch"><input type="checkbox" id="sCompact" ${d.compact_proposals ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span>
        <span><b>Компактные варианты по умолчанию</b><small>Участники пишут суть сжато, полный ответ — только председатель. Примерно в 2–3 раза дешевле; в «Параметрах» вопроса можно поменять для отдельного заседания.</small></span></label>
      <label class="field" style="max-width:20rem;margin-top:.6rem"><span>Длина компактного варианта, слов</span>
        <input type="number" id="sCompactWords" min="150" max="5000" step="50" value="${d.compact_words}" ${ro}></label>`;
  } else if (SET.tab === "facts") {
    const fc = d.fact_check;
    html = `
      <h2>Проверка фактов</h2>
      <p class="muted small">После голосования модель с поиском в интернете проверяет ключевые факты лидеров; председатель учитывает результат.</p>
      <label class="switch"><input type="checkbox" id="fEnabled" ${fc.enabled ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span><span><b>Включена по умолчанию</b></span></label>
      <div class="row2" style="margin-top:.6rem">
        <label class="field"><span>Модель с поиском</span><input type="text" id="fModel" list="modelList" value="${esc(fc.model)}" ${ro}>
          <div class="muted small">${modelInfo(fc.model)}</div></label>
        <label class="field"><span>Сколько лидеров проверять</span><input type="number" id="fTop" min="1" max="5" value="${fc.top}" ${ro}></label>
        <label class="field"><span>Утверждений на вариант</span><input type="number" id="fClaims" min="1" max="10" value="${fc.max_claims}" ${ro}></label>
      </div>
      <h2 style="margin-top:1.2rem">Голосовые сообщения</h2>
      <label class="field"><span>Модель для расшифровки речи <em>(должна понимать звук — 🎤 в подсказке)</em></span>
        <input type="text" id="tModel" list="modelList" value="${esc(d.transcribe_model)}" ${ro}>
        <div class="muted small">${modelInfo(d.transcribe_model)}</div></label>`;
  } else if (SET.tab === "brand") {
    const b = d.branding;
    html = `
      <p class="muted small">Оформление отчётов PDF, Word и Markdown. Новые отчёты получат его сразу; чтобы обновить старые — «Пересобрать отчёты архива» ниже.</p>
      <div class="row2">
        <label class="field"><span>Название в шапке <em>(пусто — «Совет нейросетей»)</em></span><input type="text" id="bTitle" value="${esc(b.title)}" ${ro}></label>
        <label class="field"><span>Текст в колонтитуле <em>(например, «Конфиденциально»)</em></span><input type="text" id="bFooter" value="${esc(b.footer)}" ${ro}></label>
        <label class="field"><span>Фирменный цвет</span>
          <div class="color-row"><input type="color" id="bColorPick" value="${/^#[0-9a-f]{6}$/i.test(b.color) ? b.color : "#4545c8"}" ${ro}>
          <input type="text" id="bColor" value="${esc(b.color)}" placeholder="#0a7d5a" ${ro}></div></label>
        <div class="field"><span>Логотип <em>(PNG или JPG до 2 МБ)</em></span>
          <div class="logo-row">${b.logo ? `<img src="/api/branding/logo?t=${Date.now()}" alt="логотип" class="logo-prev">` : '<span class="muted small">не задан</span>'}
            <label class="btn secondary">Загрузить<input type="file" id="bLogo" accept="image/png,image/jpeg" hidden ${SET.logoWritable ? "" : "disabled"}></label>
            ${b.logo ? `<button type="button" class="link" id="bLogoDel" ${ro}>Убрать</button>` : ""}</div></div>
      </div>
      <button type="button" class="secondary" id="bRebuild">Пересобрать отчёты архива</button>
      <span class="muted small" id="bRebuildState"></span>`;
  } else if (SET.tab === "system") {
    html = `<div id="sysBox"><div class="muted">Загрузка…</div></div>`;
  }
  $("setBody").innerHTML = html;
  bindSettings();
  requestAnimationFrame(() => markAllScroll($("setBody")));
  if (SET.tab === "system") renderSystem(true);
  if (SET.tab === "members" && SET.upgrades) renderUpgrades();
}

function bindSettings() {
  const d = SET.data;
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  const num = (v) => (v === "" ? null : Number(v));
  for (const el of $("setBody").querySelectorAll("[data-mi]")) {
    el.addEventListener(el.type === "checkbox" ? "change" : "input", () => {
      const m = d.members[+el.dataset.mi];
      const f = el.dataset.f;
      m[f] = el.type === "checkbox" ? el.checked : (f === "reasoning_effort" ? (el.value || null) : el.value.trim());
      if (f === "model") { const info = $("setBody").querySelector(`[data-info="${el.dataset.mi}"]`); if (info) info.innerHTML = modelInfo(m.model); }
      if (f === "enabled" || f === "name") { fixRoles(); setDirty(); renderSettings(); return; }
      setDirty();
    });
  }
  for (const b of $("setBody").querySelectorAll("[data-del]")) {
    b.addEventListener("click", async () => {
      const m = d.members[+b.dataset.del];
      if (!(await ask({ title: `Удалить участника «${m.name}»?`, html: "<p>Если нужно на время — просто выключите его переключателем.</p>", ok: "Удалить", danger: true }))) return;
      d.members.splice(+b.dataset.del, 1); fixRoles(); renderSettings(); setDirty();
    });
  }
  on("addMember", "click", () => {
    let n = 1; while (d.members.some((m) => m.name === `model${n}`)) n++;
    d.members.push({ name: `model${n}`, model: "", enabled: true, reasoning_effort: null, temperature: null, max_tokens: null, _new: true });
    renderSettings(); setDirty();
  });
  on("sChair", "change", (e) => { d.chair = e.target.value; setDirty(); });
  on("sDevil", "change", (e) => { d.devil_advocate = e.target.value || null; setDirty(); });
  on("sChairProposes", "change", (e) => { d.chair_proposes = e.target.checked; setDirty(); });
  on("sChairFollows", "change", (e) => { d.chair_follows_vote = e.target.checked; setDirty(); });
  on("checkUpgrades", "click", checkUpgrades);
  const bindVal = (id, fn) => on(id, id.startsWith("f") && $(id) && $(id).type === "checkbox" ? "change" : "input", (e) => { fn(e.target); setDirty(); });
  bindVal("sMode", (t) => { d.mode = t.value; });
  bindVal("sRounds", (t) => { d.debate_rounds = num(t.value); });
  bindVal("sBudget", (t) => { d.max_cost_usd = num(t.value); });
  bindVal("sQuorum", (t) => { d.min_quorum = num(t.value); });
  bindVal("sEffort", (t) => { d.reasoning_effort = t.value; });
  bindVal("sMaxTokens", (t) => { d.max_tokens = num(t.value); });
  bindVal("sTemp", (t) => { d.temperature = num(t.value); });
  bindVal("sLang", (t) => { d.language = t.value; });
  on("sCompact", "change", (e) => { d.compact_proposals = e.target.checked; setDirty(); });
  bindVal("sCompactWords", (t) => { d.compact_words = num(t.value); });
  on("fEnabled", "change", (e) => { d.fact_check.enabled = e.target.checked; setDirty(); });
  bindVal("fModel", (t) => { d.fact_check.model = t.value.trim(); });
  bindVal("fTop", (t) => { d.fact_check.top = num(t.value); });
  bindVal("fClaims", (t) => { d.fact_check.max_claims = num(t.value); });
  bindVal("tModel", (t) => { d.transcribe_model = t.value.trim(); });
  bindVal("bTitle", (t) => { d.branding.title = t.value; });
  bindVal("bFooter", (t) => { d.branding.footer = t.value; });
  bindVal("bColor", (t) => { d.branding.color = t.value.trim(); });
  on("bColorPick", "input", (e) => { d.branding.color = e.target.value; $("bColor").value = e.target.value; setDirty(); });
  on("bLogo", "change", uploadLogo);
  on("bRebuild", "click", async () => {
    if (setDirty() && !(await ask({ title: "Пересобрать отчёты?", html: "<p>Есть несохранённые изменения — отчёты соберутся по уже сохранённым настройкам.</p>", ok: "Пересобрать" }))) return;
    try {
      const r = await api("/api/reports/rebuild", { method: "POST" });
      $("bRebuildState").textContent = `Запущено: ${r.sessions} заседаний, отчёты обновятся в фоне за несколько минут.`;
    } catch (e) { $("bRebuildState").textContent = e.message; }
  });
  on("bLogoDel", "click", () => { d.branding.logo = ""; renderSettings(); setDirty(); });
}

function fixRoles() {
  const d = SET.data;
  const on = d.members.filter((m) => m.enabled).map((m) => m.name);
  if (!on.includes(d.chair) && on.length) d.chair = on[0];
  if (d.devil_advocate && !on.includes(d.devil_advocate)) d.devil_advocate = null;
}

async function saveSettings() {
  const d = SET.data;
  const body = { ...d, members: d.members.map(({ _new, ...m }) => m) };
  $("setSave").disabled = true;
  $("setState").textContent = "Сохраняю…";
  try {
    const r = await api("/api/settings", { method: "PUT", body });
    SET.data = r.settings; SET.orig = JSON.stringify(r.settings);
    notice("✓ Сохранено. Изменения действуют со следующего заседания."
      + (r.warnings && r.warnings.length ? `<ul>${r.warnings.map((w) => `<li>⚠️ ${esc(w)}</li>`).join("")}</ul>` : ""),
      r.warnings && r.warnings.length ? "warn" : "ok");
    renderSettings();
    try { await initForm(); } catch { /* форма обновится при следующем открытии */ }
  } catch (e) {
    notice(`Не сохранено: ${esc(e.message)}`, "bad");
  }
  setDirty();
}
$("setSave").addEventListener("click", saveSettings);

async function uploadLogo(e) {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const res = await fetch("/api/branding/logo", { method: "POST", body: f });
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.detail || `HTTP ${res.status}`);
    SET.data.branding.logo = r.logo;
    const orig = JSON.parse(SET.orig); orig.branding.logo = r.logo; SET.orig = JSON.stringify(orig);
    notice("✓ Логотип загружен.", "ok");
    renderSettings(); setDirty();
  } catch (err) { notice(`Логотип не загружен: ${esc(err.message)}`, "bad"); }
}

async function checkUpgrades() {
  $("upgrades").innerHTML = '<div class="muted">Проверяю каталог OpenRouter…</div>';
  try {
    const r = await api("/api/models/upgrades");
    if (!r.available) { $("upgrades").innerHTML = '<div class="error">Каталог OpenRouter сейчас недоступен.</div>'; return; }
    SET.upgrades = r.items;
    renderUpgrades();
  } catch (e) { $("upgrades").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

function renderUpgrades() {
  const box = $("upgrades");
  if (!box || !SET.upgrades) return;
  const row = (u, m, kind) => `<div class="upg-row"><code>${esc(m.id)}</code>
    <span class="muted small">${esc(m.date)} · ${kind} · $${m.prompt} / $${m.completion} за 1M${m.image ? " · 🖼" : ""}${m.audio ? " · 🎤" : ""}</span>
    ${SET.writable ? `<button type="button" class="secondary small-btn" data-use="${esc(u.name)}" data-model="${esc(m.id)}">Заменить</button>` : ""}</div>`;
  box.innerHTML = SET.upgrades.map((u) => {
    const cur = SET.data.members.find((m) => m.name === u.name);
    const rows = u.same_family.map((m) => row(u, m, "того же семейства")).concat(u.newest.map((m) => row(u, m, "новинка разработчика")));
    return `<div class="upg"><b>${esc(u.name)}</b>${u.enabled ? "" : ' <span class="muted small">(выключен)</span>'}:
      <code>${esc(cur ? cur.model : u.model)}</code> <span class="muted small">${u.current ? `вышла ${esc(u.current.date)}` : '<span class="bad-text">такой модели нет на OpenRouter</span>'}</span>
      ${rows.length ? rows.join("") : '<div class="muted small">новее ничего нет</div>'}</div>`;
  }).join("");
  for (const b of box.querySelectorAll("[data-use]")) {
    b.addEventListener("click", () => {
      const m = SET.data.members.find((x) => x.name === b.dataset.use);
      if (m) { m.model = b.dataset.model; renderSettings(); setDirty(); notice(`Модель участника «${esc(m.name)}» заменена на <code>${esc(m.model)}</code> — нажмите «Сохранить».`, "ok"); }
    });
  }
}

/* ------------------------------------------------------------ программа: версия, обновление, перезапуск */

async function renderSystem(first) {
  const box = $("sysBox");
  if (!box) return;
  let sys;
  try { sys = await api("/api/system"); } catch (e) {
    if (SET.restarting) { box.innerHTML = '<div class="set-notice warn">⏳ Программа перезапускается… страница обновится сама.</div>'; return; }
    box.innerHTML = `<div class="error">${esc(e.message)}</div>`; return;
  }
  SET.sys = sys;
  const v = sys.version;
  const st = sys.status || {};
  const running = st.state === "running" || sys.pending;
  const when = (iso) => (iso ? new Date(iso).toLocaleString("ru-RU") : "");
  const ver = v ? `<code>${esc((v.commit || "").slice(0, 7))}</code> от ${esc(when(v.date))}<div class="muted small">${esc(v.subject || "")}</div>`
    : '<span class="muted">неизвестна</span>';
  const ch = SET.check;
  let upd = "";
  if (ch && ch.error) upd = `<div class="error">${esc(ch.error)}</div>`;
  else if (ch && ch.behind === 0) upd = '<div class="set-notice ok">✓ Установлена последняя версия.</div>';
  else if (ch) upd = `<div class="set-notice warn"><b>Доступно обновление: ${ch.behind} ${plural(ch.behind, "изменение", "изменения", "изменений")}</b>
      <ul class="commits">${ch.commits.map((c) => `<li>${esc(c.title)} <span class="muted small">${esc(when(c.date))}</span></li>`).join("")}</ul></div>`;
  const stText = { running: "⏳ выполняется", done: "✓ выполнено", failed: "✗ ошибка", idle: "" }[st.state] || "";
  box.innerHTML = `
    ${!sys.control ? `<div class="set-notice warn">Обновление и перезапуск из браузера ещё не включены. На сервере один раз выполните:<br>
      <code>sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install</code><br>
      <span class="small">Это включит службу, которая выполняет кнопки ниже, и даст веб-интерфейсу право сохранять настройки.</span></div>` : ""}
    <div class="sys-grid">
      <div><div class="muted small">Установленная версия</div>${ver}</div>
      <div class="sys-btns">
        <button type="button" class="secondary" id="sysCheck">Проверить обновления</button>
        <button type="button" id="sysUpdate" ${!sys.control || running ? "disabled" : ""}>Обновить программу</button>
        <button type="button" class="secondary" id="sysRestart" ${!sys.control || running ? "disabled" : ""}>Перезапустить</button>
      </div>
    </div>
    ${upd}
    ${st.action ? `<div class="sys-status"><b>${st.action === "update" ? "Обновление" : "Перезапуск"}:</b> ${stText}
      ${st.at ? `<span class="muted small">${esc(when(st.at))}</span>` : ""}${st.message && st.state === "failed" ? `<div class="bad-text">${esc(st.message)}</div>` : ""}</div>` : ""}
    ${sys.log ? `<details ${running || st.state === "failed" ? "open" : ""}><summary class="small">Журнал</summary><pre class="sys-log">${esc(sys.log)}</pre></details>` : ""}
    <p class="muted small">Обновление: программа скачивает новую версию, пересобирается и перезапускается (обычно 1–5 минут).
      Ваши настройки, отчёты и архив сохраняются. Идущие заседания прервутся — обновляйте, когда совет не заседает.</p>
    <h2 style="margin-top:1.2rem">Журналы для разбора ошибок</h2>
    <p class="muted small">Журнал программы и прерванные заседания одним архивом — его можно прислать для разбора.
      Ключи и пароли в журналах скрываются; вопросы и ответы моделей — внутри. Журнал отдельного заседания —
      в его «Журнале событий».</p>
    <a class="btn secondary" href="/api/diagnostics"><svg class="ic" aria-hidden="true"><use href="#i-file"/></svg>Скачать журналы программы</a>
    <div id="failedList" class="small" style="margin-top:.6rem"></div>
    <h2 style="margin-top:1.2rem">Резервные копии настроек</h2>
    <p class="muted small">Перед каждым сохранением прежняя версия config.yaml сохраняется — можно вернуть.</p>
    <div id="backups"><button type="button" class="link" id="showBackups">Показать копии</button></div>`;
  $("sysCheck").addEventListener("click", () => checkVersion(true));
  $("sysUpdate").addEventListener("click", () => requestAction("update"));
  $("sysRestart").addEventListener("click", () => requestAction("restart"));
  $("showBackups").addEventListener("click", showBackups);
  loadFailed();
  if (first && v && !SET.check) checkVersion(false);
  if (running && !SET.poll) startPolling();
  if (!running && SET.poll && !SET.restarting) { clearInterval(SET.poll); SET.poll = null; }
}

async function checkVersion(force) {
  try { SET.check = await api(`/api/system/check${force ? "?force=1" : ""}`); }
  catch (e) { SET.check = { error: e.message }; }
  $("updDot").hidden = !(SET.check && SET.check.behind > 0);
  if (SET.tab === "system" && view === "settings") renderSystem(false);
}

async function requestAction(action) {
  const text = action === "update"
    ? "Обновить программу? Она скачает новую версию, пересоберётся и перезапустится (1–5 минут). Идущие заседания прервутся."
    : "Перезапустить программу? Идущие заседания прервутся.";
  if (!(await ask({ title: action === "update" ? "Обновить программу?" : "Перезапустить программу?", html: `<p>${esc(text)}</p>`, ok: action === "update" ? "Обновить" : "Перезапустить" }))) return;
  try {
    await api(`/api/system/${action}`, { method: "POST" });
    SET.restarting = true;
    notice(action === "update" ? "⏳ Обновление запущено. Страница обновится сама, когда программа перезапустится." : "⏳ Перезапуск…", "warn");
    startPolling();
    renderSystem(false);
  } catch (e) { notice(esc(e.message), "bad"); }
}

function startPolling() {
  if (SET.poll) clearInterval(SET.poll);
  let sawDown = false;
  const t0 = Date.now();
  SET.poll = setInterval(async () => {
    try {
      const sys = await api("/api/system");
      const st = sys.status || {};
      if (sys.pending && st.state !== "running" && Date.now() - t0 > 60000) {
        notice("Заявка ждёт больше минуты — похоже, служба на сервере не включена. Выполните на сервере один раз: "
          + "<code>sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install</code> — заявка выполнится сразу после этого.", "warn");
      }
      if (sawDown || (SET.restarting && st.state === "done")) {
        clearInterval(SET.poll); SET.poll = null;
        if (st.state === "failed") { SET.restarting = false; notice("Операция завершилась с ошибкой — подробности в журнале ниже.", "bad"); renderSystem(false); return; }
        location.reload();
        return;
      }
      if (st.state === "failed") { clearInterval(SET.poll); SET.poll = null; SET.restarting = false;
        notice("Операция завершилась с ошибкой — подробности в журнале ниже.", "bad"); }
      if (SET.tab === "system" && view === "settings") renderSystem(false);
    } catch {
      sawDown = true;  // веб-интерфейс перезапускается
      if (SET.tab === "system" && view === "settings") $("sysBox").innerHTML = '<div class="set-notice warn">⏳ Программа перезапускается… страница обновится сама.</div>';
    }
  }, 3000);
}

async function loadFailed() {
  try {
    const r = await api("/api/diagnostics/failed");
    const box = $("failedList");
    if (!box) return;
    const st = { error: "ошибка", crash: "сбой программы", cancelled: "остановлено" };
    box.innerHTML = r.items.length ? `<b>Прерванные заседания</b><ul class="backups">${r.items.map((f) => `<li>
      ${esc(f.saved_at.replace("T", " ").slice(0, 16))} · ${esc(st[f.status] || f.status)} · ${esc(f.question.slice(0, 80))}
      <div class="muted">${esc(f.error.slice(0, 160))}</div>
      <a href="/api/sessions/${encodeURIComponent(f.id)}/diagnostics">журнал</a></li>`).join("")}</ul>` : "";
  } catch { /* не важно */ }
}

async function showBackups() {
  try {
    const r = await api("/api/settings/backups");
    if (!r.items.length) { $("backups").innerHTML = '<span class="muted small">Копий пока нет.</span>'; return; }
    const label = (n) => { const m = n.match(/config-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/); return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}:${m[6]}` : n; };
    $("backups").innerHTML = `<ul class="backups">${r.items.slice(0, 20).map((n) => `<li>${esc(label(n))}
      <button type="button" class="link" data-restore="${esc(n)}" ${SET.writable ? "" : "disabled"}>Вернуть</button></li>`).join("")}</ul>`;
    for (const b of $("backups").querySelectorAll("[data-restore]")) {
      b.addEventListener("click", async () => {
        if (!(await ask({ title: "Вернуть настройки?", html: `<p>Настройки от ${esc(label(b.dataset.restore))} заменят текущие. Копия текущих тоже сохранится.</p>`, ok: "Вернуть" }))) return;
        try { await api(`/api/settings/restore/${encodeURIComponent(b.dataset.restore)}`, { method: "POST" });
          notice("✓ Настройки возвращены.", "ok"); await openSettings(); SET.tab = "system"; renderSettings();
        } catch (e) { notice(esc(e.message), "bad"); }
      });
    }
  } catch (e) { $("backups").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------ вкладки и вход */

$("setTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  SET.tab = b.dataset.tab;
  renderSettings();
});
$("settingsBtn").addEventListener("click", () => {
  if (view === "settings") return;
  openSettings();
});
window.addEventListener("beforeunload", (e) => {
  if (view === "settings" && SET.data && JSON.stringify(SET.data) !== SET.orig) { e.preventDefault(); e.returnValue = ""; }
});
// раз в сессию — тихо проверить, нет ли новой версии (точка на кнопке «Настройки»)
setTimeout(async () => {
  try {
    const sys = await api("/api/system");
    if (sys.version) await checkVersion(false);
  } catch { /* без проверки */ }
}, 4000);
