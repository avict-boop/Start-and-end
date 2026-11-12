/* Страница «Настройки»: участники и модели, параметры заседания, проверка фактов, оформление, версия программы.
   Использует общие функции из app.js: $, esc, api, showView, closeStream, markActiveHistory, initForm. */

const SET = { data: null, orig: "", tab: "members", writable: true, logo: "", models: null, upgrades: null,
  sys: null, check: null, poll: null, restarting: false };
const EFFORTS = [["", "как в общих"], ["none", "не задавать"], ["low", "низкая"], ["medium", "средняя"], ["high", "высокая"]];
const MODES = [["debate", "Подробное обсуждение"], ["research", "Исследование"], ["quick", "Быстрый ответ"]];

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
  go("#/settings");
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
  return `${esc(m.date)} · $${m.prompt} / $${m.completion} за 1M${m.image ? " · 🖼" : ""}${m.audio ? " · 🎤" : ""}`
    + `<div class="m-tags">${tagChips(m.tags)}<button type="button" class="link small" data-card="${esc(id)}">О модели…</button></div>`;
}

function tagChips(tags) {
  return (tags || []).map(([k, t]) => `<span class="badge ${k === "plain" ? "plain" : k}">${esc(t)}</span>`).join("");
}

// карточка модели: метки, для каких ролей подходит, ваша история, описание разработчика и пересказ по-русски
async function openModelCard(id) {
  let c;
  try { c = await api(`/api/models/card?model=${encodeURIComponent(id)}`); } catch (e) { toast(e.message); return; }
  const r = c.row || {};
  const k = (n) => (n == null ? "—" : n >= 1000 ? `${Math.round(n / 1000)} тыс.` : String(n));
  const h = c.history;
  const hist = h ? `<ul class="card-list"><li>заседаний: <b>${h.sessions}</b>${h.cost_per_session != null ? `, в среднем $${h.cost_per_session} за заседание` : ""}</li>
      ${h.proposals ? `<li>вариант модели победил: <b>${h.wins} из ${h.proposals}</b>${h.win_rate != null ? ` (${Math.round(h.win_rate * 100)}%)` : ""}${h.avg_score != null ? `, средняя оценка критиков ${h.avg_score}` : ""}</li>` : ""}
      ${h.by_mode && Object.keys(h.by_mode).length > 1 ? `<li>по режимам: ${Object.entries(h.by_mode).filter(([, b]) => b.proposals)
        .map(([m, b]) => `${({ debate: "обсуждение", quick: "быстрый", evaluate: "сравнение", research: "исследование", creative: "креатив" })[m] || m} — ${b.wins} из ${b.proposals}`).join("; ")}</li>` : ""}
      <li>сбоев: <b>${h.failures}</b>${h.reasoning_off ? `; рассуждения пришлось выключать в ${h.reasoning_off} заседаниях` : ""}</li></ul>`
    : '<p class="muted small">В ваших заседаниях эта модель ещё не участвовала.</p>';
  const html = !c.found ? `<p class="bad-text">${c.available ? "Такой модели сейчас нет в каталоге OpenRouter." : "Каталог OpenRouter недоступен."}</p>${hist}` : `
    <div class="m-tags">${tagChips(c.tags)}</div>
    <p class="muted small">${esc(r.date)} · $${r.prompt} вход / $${r.completion} выход за 1M токенов · контекст ${k(r.context)}${r.max_out ? ` · ответ до ${k(r.max_out)}` : ""}</p>
    <h4>Для чего подходит в совете</h4><ul class="card-list">${(c.roles || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <p class="muted small">Подсказка по характеристикам модели, а не оценка её качества: качество лучше видно по вашей истории ниже.</p>
    <h4>В ваших заседаниях</h4>${hist}
    <h4>Описание разработчика</h4>
    <p id="cardDesc">${c.description_tr ? esc(c.description_tr) : c.description ? '<span class="muted small">Перевожу описание…</span>' : ""}</p>
    <div id="cardSummary">${c.summary ? `<p>${esc(c.summary)}</p><p class="muted small">Пересказ по описанию разработчика (сделан моделью).</p>` : ""}</div>
    ${c.description && !c.summary ? '<button type="button" class="secondary small-btn" id="cardSum">Кратко по-русски</button> <span class="muted small">доли цента, запоминается</span>' : ""}
    ${c.description ? `<details class="card-desc"><summary>Оригинальный текст разработчика</summary><p>${esc(c.description)}</p></details>` : '<p class="muted small">Описания в каталоге нет.</p>'}`;
  const done = ask({ title: id, html, ok: "Закрыть", cancel: null, wide: true });
  if (c.description && !c.description_tr) {
    api("/api/models/translate", { method: "POST", body: { ids: [id], full: true } }).then((t) => {
      const el = document.getElementById("cardDesc");
      if (!el) return;
      const tr = (t.translations || {})[id];
      el.innerHTML = tr ? esc(tr) : `<span class="muted small">${esc(t.unavailable || "перевод недоступен")} — оригинал ниже.</span>`;
    }).catch(() => { const el = document.getElementById("cardDesc"); if (el) el.innerHTML = '<span class="muted small">Перевод недоступен — оригинал ниже.</span>'; });
  }
  const btn = document.getElementById("cardSum");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "Пересказываю…";
    try {
      const s = await api("/api/models/card/summary", { method: "POST", body: { model: id } });
      document.getElementById("cardSummary").innerHTML = `<p>${esc(s.summary)}</p><p class="muted small">Пересказ по описанию разработчика (сделан моделью).</p>`;
      btn.remove();
    } catch (e) { btn.disabled = false; btn.textContent = "Кратко по-русски"; toast(e.message); }
  });
  await done;
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
        «Лимит ответа» — сколько токенов модель может потратить на ответ вместе со скрытыми рассуждениями (пусто — как в общих настройках);
        думающим моделям с глубиной «средняя» или «высокая» лучше 16 000–24 000.
        Изменения действуют со следующего заседания.</p>
      <div class="table-wrap"><table class="table set-members">
        <thead><tr><th>Вкл.</th><th>Имя</th><th>Модель</th><th>Глубина рассуждений</th><th title="Сколько токенов модель может потратить на ответ вместе со скрытыми рассуждениями">Лимит ответа</th><th><span class="sr-only">Удалить</span></th></tr></thead>
        <tbody>${d.members.map((m, i) => `<tr class="${m.enabled ? "" : "off"}">
          <td class="c-on"><label class="switch small" title="Участвует в заседаниях"><input type="checkbox" data-mi="${i}" data-f="enabled" ${m.enabled ? "checked" : ""} ${ro} aria-label="${esc(m.name)} участвует"><span class="sw" aria-hidden="true"></span></label></td>
          <td class="c-name" data-th="Имя"><input type="text" data-mi="${i}" data-f="name" value="${esc(m.name)}" ${m._new ? "" : "readonly"} ${ro} class="w-name" aria-label="Имя участника"></td>
          <td data-th="Модель на OpenRouter"><input type="text" data-mi="${i}" data-f="model" value="${esc(m.model)}" list="modelList" ${ro} class="w-model" aria-label="Модель ${esc(m.name)}">
            <div class="muted small" data-info="${i}">${modelInfo(m.model)}</div>
            ${SET.writable && m.model.includes("/") ? `<button type="button" class="link small" data-family="${i}" title="Выбрать другую модель этого разработчика — с описанием">Заменить модель</button>` : ""}</td>
          <td data-th="Глубина рассуждений">${sel(`eff${i}`, EFFORTS, m.reasoning_effort || "", `data-mi="${i}" data-f="reasoning_effort" ${ro} aria-label="Глубина рассуждений ${esc(m.name)}"`)}</td>
          <td data-th="Лимит ответа, токенов"><input type="number" min="500" max="128000" step="1000" data-mi="${i}" data-f="max_tokens" value="${m.max_tokens ?? ""}" placeholder="${d.max_tokens}" ${ro} class="w-tok" aria-label="Лимит ответа ${esc(m.name)}"></td>
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
        <label class="field"><span>Раундов критики</span><input type="number" id="sRounds" min="0" max="5" value="${d.debate_rounds}" ${ro}></label>
        <label class="field"><span>Лимит заседания, $</span><input type="number" id="sBudget" min="0.01" step="any" value="${d.max_cost_usd}" ${ro}></label>
        <label class="field"><span>Кворум</span><input type="number" id="sQuorum" min="1" value="${d.min_quorum}" ${ro}><small class="hint">минимум вариантов, чтобы продолжить</small></label>
        <label class="field"><span>Глубина рассуждений</span>${sel("sEffort", EFFORTS.slice(1), d.reasoning_effort, ro)}<small class="hint">для «думающих» моделей</small></label>
        <label class="field"><span>Лимит длины ответа, токенов</span><input type="number" id="sMaxTokens" min="500" step="500" value="${d.max_tokens}" ${ro}><small class="hint">платится только написанное; при меньшем пределе длинные ответы дописываются продолжениями, и каждое заново читает весь вход — рекомендуем 16000</small></label>
        <label class="field"><span>Температура</span><input type="number" id="sTemp" min="0" max="2" step="0.1" value="${d.temperature}" ${ro}><small class="hint">0 — строже, 1 — креативнее</small></label>
        <label class="field"><span>Язык ответов</span><input type="text" id="sLang" value="${esc(d.language)}" ${ro}><small class="hint">по-английски: Russian, English…</small></label>
      </div>
      <h2 style="margin-top:1rem">Экономия</h2>
      <label class="switch"><input type="checkbox" id="sCompact" ${d.compact_proposals ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span>
        <span><b>Компактные варианты по умолчанию</b><small>Участники пишут суть сжато, полный ответ — только председатель. Примерно в 2–3 раза дешевле; в «Параметрах» вопроса можно поменять для отдельного заседания.</small></span></label>
      <label class="field" style="max-width:20rem;margin-top:.6rem"><span>Длина компактного варианта, слов</span>
        <input type="number" id="sCompactWords" min="150" max="5000" step="50" value="${d.compact_words}" ${ro}></label>
      <h2 style="margin-top:1rem">Надёжность и пределы</h2>
      <p class="muted small">Значения по умолчанию — по рекомендациям аудита совета. Менять стоит, если видно, что мешают.</p>
      ${[["quick_keep", "Идеи проигравших в «Быстром ответе»", "голосующие называют ценное из вариантов не на первом месте — его не теряем"],
         ["evaluate_allow_none", "«Ни один вариант не подходит» в «Выборе из вариантов»", "председатель может честно сказать, что все варианты непригодны, с причиной и цитатой"],
         ["chair_critiques", "Председатель участвует в критике", "эксперимент пилота: выключите — дешевле, но у каждой пары вариантов меньше оценок"],
         ["chair_votes", "Председатель голосует", "эксперимент пилота: выключите — дешевле, но меньше бюллетеней"],
         ["web_tools", "Поиск через инструмент OpenRouter", "новый способ поиска (web_search); выключите, только если поиск у участников перестал работать — вернётся прежний плагин"],
         ["json_mode", "Строгий JSON в ответах", "модели, которые умеют, отвечают строго JSON — меньше платных повторов из-за сломанного формата; кто не умеет — без этого"],
         ["key_reserve", "Общий учёт лимита ключа", "сайт и бот вместе следят за остатком лимита OpenRouter: пауза до того, как ключ откажет посреди этапа"],
         ["budget_reserve", "Резерв бюджета на запросы", "новый запрос не уходит, если с ним и уже отправленными бюджет будет превышен — цена каждого запроса оценивается по каталогу; пауза — чуть раньше, но без перерасхода"]].map(([k, t, h]) =>
        `<label class="switch"><input type="checkbox" data-kb="${k}" ${d[k] ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span><span><b>${t}</b><small>${h}</small></span></label>`).join("")}
      <div class="row2" style="margin-top:.6rem">
        ${[["judge_temperature", "Температура оценок и итога", "0", "2", "0.1", "постановка, критика, голосование, итог; пусто — как у авторов"],
           ["verdict_max_tokens", "Предел ответа председателя на итоге, токенов", "0", "64000", "1000", "длинный итог одним ответом, без склеек; 0 — общий предел"],
           ["max_requirements", "Обязательных требований из запроса, до", "1", "40", "1", "длинные ТЗ бывают с 10+ требованиями"],
           ["quorum_large", "Кворум при 4+ авторах", "2", "10", "1", "в обсуждении и исследовании; при двух вариантах выбор по сути делает председатель"],
           ["max_concurrency", "Запросов к моделям одновременно", "1", "16", "1", "меньше — меньше перерасход у границы бюджета, но дольше"],
           ["reserve_output_share", "Резерв: доля предела ответа", "0.1", "1", "0.1", "сколько от предела ответа модели закладывать в резерв; 1 — худший случай"],
           ["reports_memory_mb", "Память для сборки отчётов, МБ", "0", "16000", "50", "реально занятая память; превысит — сборка остановится с ошибкой, сайт работает; 0 — без предела"]].map(([k, t, mn, mx, st, h]) =>
          `<label class="field"><span>${t}</span><input type="number" data-k="${k}" min="${mn}" max="${mx}" step="${st}" value="${d[k] ?? ""}" ${ro}><small class="hint">${h}</small></label>`).join("")}
      </div>
      <label class="field" style="max-width:360px"><span>Поисковик у авторов</span>
        <select id="sEngine" ${ro}>${[["exa", "Exa через OpenRouter — с лимитом (рекомендуем)"], ["auto", "встроенный поиск провайдера, где есть"], ["parallel", "Parallel"], ["perplexity", "Perplexity Search"]]
          .map(([v, t]) => `<option value="${v}" ${(d.search_engine ?? "exa") === v ? "selected" : ""}>${t}</option>`).join("")}</select>
        <small class="hint">Exa соблюдает лимит: не больше 3 поисков по 5 результатов за запрос. Встроенный поиск OpenAI и Google лимитов не знает — автор может прочитать миллионы токенов (12.11: $1,14 за один запрос)</small></label>
      <div class="row2">
        <label class="field"><span>Поисков у автора за запрос</span><input type="number" data-k="search_uses" min="1" max="10" step="1" value="${d.search_uses ?? 3}" ${ro}><small class="hint">3 — обычный вопрос; 6–10 — широкий скрининг (20–30 компаний). Каждый поиск заново читает весь запрос: вход и цена растут примерно пропорционально</small></label>
        <label class="field"><span>Результатов в каждом поиске</span><input type="number" data-k="search_results" min="1" max="10" step="1" value="${d.search_results ?? 5}" ${ro}><small class="hint">страниц на один поиск; всего за запрос — поисков × результатов</small></label>
      </div>
      <label class="field" style="max-width:360px"><span>Глубина рассуждений на голосовании</span>
        <select id="sVoteReason" ${ro}>${[["", "как у участника"], ["none", "без рассуждений"], ["low", "низкая — дешевле (рекомендуем)"], ["medium", "средняя"], ["high", "высокая"]]
          .map(([v, t]) => `<option value="${v}" ${(d.vote_reasoning ?? "low") === v ? "selected" : ""}>${t}</option>`).join("")}</select>
        <small class="hint">голосование — ранжирование готовых вариантов; глубокие скрытые рассуждения здесь дороги и почти не нужны</small></label>
      <h2 style="margin-top:1rem">Журнал программы</h2>
      <div class="row2">
        <label class="field"><span>Подробность</span>
          <select id="sLogLevel" ${ro}>${[["debug", "максимально подробно (каждый запрос к модели и действие)"], ["info", "главное"], ["warning", "только сбои"]]
            .map(([v, t]) => `<option value="${v}" ${(d.log_level ?? "debug") === v ? "selected" : ""}>${t}</option>`).join("")}</select>
          <small class="hint">runs/logs/web.log и bot.log; скачать — «Программа → Скачать журналы программы». Уровень на сайте меняется сразу, у бота — после перезапуска. Ключи и токены в журнал не попадают</small></label>
        <label class="field"><span>Размер файла журнала, МБ</span><input type="number" data-k="log_file_mb" min="1" max="200" step="1" value="${d.log_file_mb ?? 10}" ${ro}></label>
        <label class="field"><span>Сколько файлов хранить</span><input type="number" data-k="log_files" min="2" max="50" step="1" value="${d.log_files ?? 10}" ${ro}><small class="hint">старые удаляются сами; новый размер — после перезапуска</small></label>
      </div>
      <p class="muted small" id="reportPeaks"></p>`;
    loadReportPeaks();
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
        <label class="field"><span>Ссылок автора открывает программа</span><input type="number" id="fLeads" min="0" max="5" value="${fc.read_leads === false ? 0 : (fc.lead_pages ?? 2)}" ${ro}><small class="hint">программа сама скачивает страницы, на которые сослался автор утверждения, и даёт модели проверки нужные отрывки — поиск модели часто не находит сайты компаний на турецком, китайском и других языках. Бесплатно, до пары минут; 0 — не открывать</small></label>
        <label class="field"><span>Страниц читать целиком</span><input type="number" id="fFetch" min="0" max="10" value="${fc.fetch_pages ?? 3}" ${ro}><small class="hint">модель проверки открывает страницу, а не довольствуется фрагментом выдачи (около $0,01 за страницу); 0 — не читать</small></label>
      </div>
      <div class="row2" style="margin-top:.4rem"><button type="button" class="secondary" id="searchTest">Проверить поиск</button>
        <span class="muted small" id="searchTestOut">Один короткий запрос к модели с поиском (обычно меньше цента): работает ли поиск, сколько стоит и возвращает ли он ссылки.</span></div>
      <h2 style="margin-top:1.2rem">Режим «Исследование»</h2>
      <p class="muted small">Каждую часть можно выключить, чтобы на пилоте сравнить, что помогает.</p>
      ${[["rLenses", "lenses", "Углы зрения авторов", "у каждого автора своя линза: рынок, технология, экономика, скептик"],
         ["rArb", "arbitration", "Арбитраж доказательств", "после критики модель с поиском проверяет утверждения всех вариантов"],
         ["rDigest", "revision_digest", "Доработка по выжимкам", "авторы видят реестр источников и тезисы чужих вариантов, а не их полные тексты"],
         ["rRevSearch", "revision_search", "Поиск и на доработке", "авторы с поиском ищут и при доработке; дорого — каждый поиск заново читает весь контекст (чужие варианты, критику, реестр). Выключено: доработка по реестру проверенных источников"],
         ["rSection", "section_vote", "Голосование по подвопросам", "участники выбирают лучший вариант для каждого подвопроса"],
         ["rShorten", "shorten_long", "Сокращать слишком длинные варианты", "вариант намного длиннее заданного возвращается автору на сокращение (по умолчанию; можно поменять для отдельного вопроса)"],
         ["rDedupe", "dedupe_claims", "Объединять одинаковые утверждения", "одно и то же утверждение разных авторов проверяется поиском один раз"],
         ["rDefer", "defer_private", "Непубличное — сразу в «неизвестно»", "права, внутренние цены и «нигде нет» поиском не решить: не тратим на них проверки, пишем, где узнать"],
         ["rCover", "coverage_first", "Сначала покрыть подвопросы", "на проверку — по утверждению на каждый подвопрос, потом по автору, потом самые важные"],
         ["rVoteLedger", "vote_ledger", "Голосующие видят реестр источников", "что подтверждено, а что «из памяти» — при выборе лучшего варианта"],
         ["rCard", "decision_card", "Карточка решения", "председатель пишет решение на одну страницу — оно первым в итоге"],
         ["rMark", "mark_unconfirmed", "Помечать неподтверждённое в тексте итога", "если «известно» без источника перенесено в «неизвестно» — предупреждение в начале итога, пометка на месте и полный список"],
         ["rClaimIds", "require_claim_ids", "«Известно» — только с проверенным утверждением", "каждый пункт называет утверждения реестра, на которые опирается; без них — в «неизвестно»"],
         ["rNums", "known_numbers_check", "Не расширять проверенное", "в «известно» нет чисел, которых нет в проверенном утверждении; иначе — в «неизвестно» с причиной"],
         ["rDecisive", "check_decisive_private", "Решающее «непубличное» — проверять", "автор мог ошибиться с меткой: правообладатель и регистрация часто публичны — один публичный поиск"],
         ["rSemantic", "semantic_audit", "Теневая сверка смысла «известно»", "одна модель без поиска проверяет, следует ли каждый пункт из своей опоры; только показывается, статус не меняет (центы)"],
         ["rQuote", "quote_check", "Искать цитату на странице", "поиск приводит дословную фразу источника, программа находит её на самой странице; для важного утверждения не нашлась — не засчитано (бесплатно)"],
         ["rLinks", "link_check", "Проверять ссылки", "открыть каждую ссылку реестра: мёртвая ничего не подтверждает"]].map(([id, k, t, h]) =>
        `<label class="switch"><input type="checkbox" id="${id}" data-r="${k}" ${d.research[k] ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span><span><b>${t}</b><small>${h}</small></span></label>`).join("")}
      <div class="row2" style="margin-top:.6rem">
        <label class="field"><span>Поисковых заданий (подтвердить / опровергнуть)</span><input type="number" id="rMissions" data-rn="missions" min="1" max="2" value="${d.research.missions}" ${ro}></label>
        <label class="field"><span>Утверждений на проверку</span><input type="number" id="rClaims" data-rn="arbitration_claims" min="3" max="30" value="${d.research.arbitration_claims}" ${ro}></label>
        <label class="field"><span>Авторов с поиском по умолчанию (исследование)</span><input type="number" id="rScouts" data-rn="fresh_scouts" min="0" max="2" value="${d.research.fresh_scouts}" ${ro}>
          <div class="muted small">начальное значение «Свежих данных» на экране вопроса в исследовании — вместе с участниками, у которых поиск включён в «Моделях»; для вопроса меняется там. 0 — независимо и дешевле</div></label>
        <label class="field"><span>Длина варианта, слов</span><input type="number" id="rWords" data-rn="max_words" min="0" max="5000" step="100" value="${d.research.max_words}" ${ro}><small class="hint">0 — без ограничения длины (или от 300)</small>
          <div class="muted small">по умолчанию; в «Параметрах» вопроса можно поменять для отдельного исследования</div></label>
        <label class="field"><span>«Слишком длинный» — во сколько раз длиннее</span><input type="number" id="rFactor" data-rn="long_factor" min="1.2" max="5" step="0.1" value="${d.research.long_factor}" ${ro}></label>
        <label class="field"><span>«Не удалось проверить» — второй поиск, до</span><input type="number" data-rn="recheck_unclear" min="0" max="15" value="${d.research.recheck_unclear}" ${ro}>
          <div class="muted small">утверждений; ищутся другими запросами; 0 — без второго поиска</div></label>
        <label class="field"><span>Новые утверждения после доработки — проверить, до</span><input type="number" data-rn="post_revision_claims" min="0" max="10" value="${d.research.post_revision_claims}" ${ro}></label>
        <label class="field"><span>Фрагмент страницы — не подтверждение</span>
          <select data-rs="snippet_rule" ${ro}>${[["critical", "для спорных и быстро меняющихся"], ["all", "для всех утверждений"], ["off", "не применять"]].map(([v, t]) => `<option value="${v}" ${d.research.snippet_rule === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label class="field"><span>Утверждений в варианте, до</span><input type="number" data-rn="max_claims" min="3" max="15" value="${d.research.max_claims}" ${ro}></label>
        <label class="field"><span>Строк таблиц в выжимке для доработки</span><input type="number" data-rn="digest_table_rows" min="0" max="40" value="${d.research.digest_table_rows}" ${ro}></label>
        <label class="field"><span>Предел ответа автора: токенов на слово</span><input type="number" data-rn="author_tokens_per_word" min="0" max="20" step="0.5" value="${d.research.author_tokens_per_word}" ${ro}>
          <div class="muted small">× длина варианта; 0 — общий предел</div></label>
        <label class="field"><span>…и не меньше, токенов (и не меньше общего лимита ответа)</span><input type="number" data-rn="author_min_tokens" min="1000" max="64000" step="500" value="${d.research.author_min_tokens}" ${ro}>
          <div class="muted small">думающим моделям нужно место на скрытые рассуждения</div></label>
      </div>
      <h2 style="margin-top:1.2rem">Пилот</h2>
      <label class="field"><span>Модель для «ответа одной модели с поиском»</span><input type="text" list="modelList" id="pSolo" value="${esc(d.pilot_solo_model || "")}" ${ro}>
        <div class="muted small">контрольная группа к совету: если одиночный ответ не хуже — совет не окупается; пусто — модель председателя</div></label>
      <h2 style="margin-top:1.2rem">Подбор состава</h2>
      <p class="muted small">Кнопка «✨ Подобрать состав» в форме вопроса.</p>
      <label class="switch"><input type="checkbox" data-ab="dossier" ${d.advisor.dossier ? "checked" : ""} ${ro}><span class="sw" aria-hidden="true"></span>
        <span><b>Обзоры моделей из интернета</b><small>советник опирается на найденные обзоры и известные проблемы моделей</small></span></label>
      <div class="row2" style="margin-top:.6rem">
        <label class="field"><span>Модель-советник</span><input type="text" list="modelList" data-as="model" value="${esc(d.advisor.model)}" ${ro}><div class="muted small">пусто — модель председателя</div></label>
        ${[["dossier_days", "Обзор свежий, дней", "1", "180"], ["max_candidates", "Моделей каталога на выбор", "6", "60"],
           ["question_chars", "Знаков вопроса видит советник", "2000", "200000"], ["history_min_sessions", "Ваша история — довод, с заседаний", "1", "100"],
           ["max_arbitration_claims", "Потолок «утверждений на проверку»", "3", "30"], ["max_words_cap", "Потолок длины варианта, слов", "300", "5000"]].map(([k, t, mn, mx]) =>
          `<label class="field"><span>${t}</span><input type="number" data-an="${k}" min="${mn}" max="${mx}" value="${d.advisor[k]}" ${ro}></label>`).join("")}
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
        <label class="field"><span>Название в шапке</span><input type="text" id="bTitle" value="${esc(b.title)}" ${ro}><small class="hint">пусто — «Совет нейросетей»</small></label>
        <label class="field"><span>Текст в колонтитуле</span><input type="text" id="bFooter" value="${esc(b.footer)}" ${ro}><small class="hint">например, «Конфиденциально»</small></label>
        <label class="field"><span>Фирменный цвет</span>
          <div class="color-row"><input type="color" id="bColorPick" value="${/^#[0-9a-f]{6}$/i.test(b.color) ? b.color : "#4f46e5"}" ${ro}>
          <input type="text" id="bColor" value="${esc(b.color)}" placeholder="#0a7d5a" ${ro}></div></label>
        <div class="field"><span>Логотип <em>— PNG или JPG до 2 МБ</em></span>
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
      m[f] = el.type === "checkbox" ? el.checked : f === "reasoning_effort" ? (el.value || null)
        : f === "max_tokens" ? (el.value ? Number(el.value) : null) : el.value.trim();
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
  for (const b of $("setBody").querySelectorAll("[data-family]")) b.addEventListener("click", () => pickFromFamily(+b.dataset.family));
  $("setBody").addEventListener("click", (e) => { const b = e.target.closest("[data-card]"); if (b) openModelCard(b.dataset.card); });
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
  bindVal("fFetch", (t) => { d.fact_check.fetch_pages = num(t.value); });
  bindVal("fLeads", (t) => { const n = num(t.value) || 0; d.fact_check.lead_pages = n; d.fact_check.read_leads = n > 0; });
  bindVal("sVoteReason", (t) => { d.vote_reasoning = t.value; });
  bindVal("sEngine", (t) => { d.search_engine = t.value; });
  bindVal("sLogLevel", (t) => { d.log_level = t.value; });
  bindVal("tModel", (t) => { d.transcribe_model = t.value.trim(); });
  bindVal("pSolo", (t) => { d.pilot_solo_model = t.value.trim(); });
  for (const el of $("setBody").querySelectorAll("[data-r]")) el.addEventListener("change", () => { d.research[el.dataset.r] = el.checked; setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-rn]")) el.addEventListener("input", () => { d.research[el.dataset.rn] = num(el.value); setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-rs]")) el.addEventListener("change", () => { d.research[el.dataset.rs] = el.value; setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-kb]")) el.addEventListener("change", () => { d[el.dataset.kb] = el.checked; setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-k]")) el.addEventListener("input", () => { d[el.dataset.k] = num(el.value); setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-ab]")) el.addEventListener("change", () => { d.advisor[el.dataset.ab] = el.checked; setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-an]")) el.addEventListener("input", () => { d.advisor[el.dataset.an] = num(el.value); setDirty(); });
  for (const el of $("setBody").querySelectorAll("[data-as]")) el.addEventListener("input", () => { d.advisor[el.dataset.as] = el.value.trim(); setDirty(); });
  on("searchTest", "click", async () => {
    const out = $("searchTestOut");
    out.textContent = "Проверяю…";
    try {
      const r = await api("/api/search-test", { method: "POST", body: { model: d.fact_check.model } });
      out.innerHTML = `<b>${r.ok ? "Поиск работает" : "Поиск не сработал"}</b> · ${esc(r.model || "")} · $${Number(r.cost || 0).toFixed(4)} · ссылок от поиска: ${r.citations.length}`
        + (r.citations.length ? "" : " (поиск не вернул список ссылок — в арбитраже ссылки будут приниматься по ответу модели)")
        + (r.error ? `<div class="error">${esc(r.error)}</div>` : "");
    } catch (e) { out.textContent = e.message; }
  });
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

// выбор любой доступной модели того же разработчика: линейки, новые сверху, цены, контекст, рассуждения
// выбор любой модели того же разработчика (по линейкам, с описаниями) — для настроек и для отдельного вопроса
async function chooseFamilyModel(current, title, note = "") {
  let r;
  try { r = await api(`/api/models/family?model=${encodeURIComponent(current)}`); } catch (e) { toast(e.message); return null; }
  if (!r.available) { toast("Каталог OpenRouter сейчас недоступен"); return null; }
  const k = (n) => (n == null ? "—" : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n));
  const row = (x) => `<li class="fam-item${x.id === current ? " cur" : ""}" data-text="${esc((x.id + " " + x.name).toLowerCase())}">
      <div class="fam-main"><code>${esc(x.id)}</code>${x.id === current ? ' <span class="badge plain">сейчас</span>' : ""}
        <div class="muted small">${esc(x.date)} · $${x.prompt} / $${x.completion} за 1M · контекст ${k(x.context)}${x.max_out ? ` · ответ до ${k(x.max_out)}` : ""}</div>
        <div class="m-tags">${tagChips(x.tags)}</div>${x.desc ? `<div class="small fam-desc" data-desc="${esc(x.id)}">${esc(x.desc_tr || x.desc)}</div>` : ""}</div>
      ${x.id === current ? "" : `<button type="button" class="secondary small-btn" data-pick="${esc(x.id)}">Выбрать</button>`}</li>`;
  const html = `<p class="muted small">Все модели <b>${esc(r.provider)}</b>, доступные сейчас на OpenRouter, по линейкам, новые сверху.${note ? " " + note : ""}</p>
    <div class="fam-search"><input type="search" id="famSearch" placeholder="Поиск: pro, flash, r1…" aria-label="Поиск модели"></div>
    <div class="fam-list">${r.groups.map((g) => `<h4>${esc(g.family)}</h4><ul>${g.items.map(row).join("")}</ul>`).join("")}</div>`;
  let chosen = null;
  const done = ask({ title, html, ok: "Закрыть", cancel: null, wide: true });
  const untranslated = r.groups.flatMap((g) => g.items).filter((x) => x.desc && !x.desc_tr).map((x) => x.id);
  if (untranslated.length) {
    api("/api/models/translate", { method: "POST", body: { ids: untranslated } }).then((t) => {
      for (const [mid, text] of Object.entries(t.translations || {})) {
        const el = $("dBody").querySelector(`[data-desc="${CSS.escape(mid)}"]`);
        if (el) el.textContent = text;
      }
    }).catch(() => {});
  }
  const body = $("dBody");
  body.querySelector("#famSearch").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase().trim();
    for (const li of body.querySelectorAll(".fam-item")) li.hidden = !!q && !li.dataset.text.includes(q);
    for (const h of body.querySelectorAll(".fam-list h4")) h.hidden = ![...h.nextElementSibling.children].some((li) => !li.hidden);
  });
  for (const btn of body.querySelectorAll("[data-pick]")) btn.addEventListener("click", () => { chosen = btn.dataset.pick; $("dOk").click(); });
  await done;
  return chosen;
}

async function pickFromFamily(i) {
  const m = SET.data.members[i];
  const chosen = await chooseFamilyModel(m.model, `Модель для «${m.name}»`,
    "🧠 — умеет рассуждать (глубину задайте в столбце «Глубина рассуждений»).");
  if (!chosen) return;
  m.model = chosen;
  renderSettings(); setDirty();
  notice(`Модель участника «${esc(m.name)}» — <code>${esc(chosen)}</code>. Нажмите «Сохранить», а затем «Программа → Перезапустить», чтобы сбросить запомненные настройки рассуждений.`, "ok");
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

// пик памяти последних сборок отчётов — по нему подбирать предел reports_memory_mb
async function loadReportPeaks() {
  let r;
  try { r = await api("/api/reports/memory"); } catch { return; }
  const el = document.getElementById("reportPeaks");
  if (!el || !r || !r.count) return;
  el.textContent = `Сборка отчётов: пик памяти за последние ${r.count} — до ${r.max_mb} МБ (обычно ${r.median_mb} МБ)` +
    (r.killed ? `; остановлено по пределу: ${r.killed}` : "") + ". Предел стоит держать выше пика с запасом.";
}
