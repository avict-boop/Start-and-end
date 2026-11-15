"use strict";
/* Режим «Отбор» на сайте: правка списков на остановках «проверьте», таблица с ползунками весов (ранг пересчитывает
   сервер, без моделей), копия источника, выгрузка в Excel. Общие помощники ($, esc, api, toast, md, plural) —
   из app.js (он подключается после этого файла; функции вызываются позже). */

const SEL_STATUS = { document: ["ok", "Д", "документ"], official: ["plain", "С", "сайт компании"], secondary: ["warn", "В", "вторичный источник"],
  unverified: ["bad", "?", "цитата не найдена"], none: ["plain", "—", "не найдено"], disputed: ["warn", "⚖", "спор"] };
const SEL_STAGE = {
  framing: ["Рамка отбора", "председатель задаёт, кто кандидат, какие признаки сравнивать, их веса и где искать"],
  discover: ["Ищем кандидатов", "поиск по нескольким направлениям; дубли склеиваются, полнота списка оценивается"],
  screen: ["Отсев", "короткая проверка признаков отсева у всех кандидатов"],
  fill: ["Заполняем таблицу", "программа скачивает страницы, две модели заполняют ячейки, программа ищет каждую цитату"],
  dispute: ["Разбор спорного", "совет решает только ячейки, где источники расходятся"],
  verdict: ["Ранг и вывод", "ранг по весам считает программа, председатель пишет короткий вывод"],
};
// ссылки приходят от моделей и поиска: в href — только http(s), иначе «javascript:…» выполнился бы по нажатию
const selUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
// короткий заголовок колонки: до скобки или запятой, не длиннее 34 знаков (полное название — при наведении)
const selShort = (n) => { const s = String(n || "").split(/\s*[(（]|\s+—\s+|,\s/)[0].trim() || String(n || ""); return s.length > 34 ? s.slice(0, 33) + "…" : s; };
const SEL_KIND = { number: "число", bool: "да/нет", list: "список", date: "дата", text: "текст" };
const SEL_GATE_TITLE = { frame: "Проверьте признаки и веса", candidates: "Проверьте найденных кандидатов",
  shortlist: "Утвердите короткий список" };

function selEvent(s, ev) {
  // события «Отбора» в журнал хода; true — событие обработано
  const n = (x, a, b, c) => `${x} ${plural(x, a, b, c)}`;
  switch (ev.type) {
    case "selection_frame":
      s.selFrame = ev.frame;
      s.selAttrs = (ev.frame.attributes || []);
      logAdd(s, `Рамка: ${n((ev.frame.attributes || []).length, "признак", "признака", "признаков")}; `
        + (ev.start === "check" ? "проверяем ваш список" : ev.start === "extend" ? "ваш список плюс поиск недостающих" : "ищем кандидатов с нуля")
        + (ev.seeds ? ` (в задании ${n(ev.seeds, "объект", "объекта", "объектов")})` : ""), "k-ok");
      return true;
    case "candidates_found":
      logAdd(s, `${ev.by ? `${ev.by}, ` : ""}«${shortTitle(ev.angle, 60)}»: ${n(ev.count, "кандидат", "кандидата", "кандидатов")}`);
      return true;
    case "candidates": {
      const c = ev.completeness || {};
      logAdd(s, `Найдено ${n(ev.count, "кандидат", "кандидата", "кандидатов")}${ev.merged ? `, склеено дублей: ${ev.merged}` : ""}`
        + (c.share != null ? ` · полнота списка ≈ ${Math.round(c.share * 100)}% (оценка ~${c.estimate})` : ""), "k-ok");
      return true;
    }
    case "screened":
      logAdd(s, `Отсев: подходят ${ev.pass}, не подходят ${ev.fail}, не выяснено ${ev.unclear} из ${ev.total}`, "k-ok");
      return true;
    case "shortlist":
      s.selObjs = ev.objects || s.selObjs || [];
      s.selCells = s.selCells || {};
      logAdd(s, `Подробно проверяем ${n(ev.count, "объект", "объекта", "объектов")}${ev.excluded ? ` · исключено при отсеве: ${ev.excluded}` : ""}`, "k-ok");
      return true;
    case "object_filling":
      (s.selBusy = s.selBusy || {})[ev.object] = true;
      if (s.selObjs && !s.selObjs.some((o) => o.id === ev.object)) s.selObjs.push({ id: ev.object, name: ev.name });
      return true;
    case "object_ruled":
      s.selCells = s.selCells || {};
      s.selCells[ev.object] = { ...(s.selCells[ev.object] || {}), ...(ev.cells || {}) };
      return true;
    case "object_filled":
      (s.selCells = s.selCells || {})[ev.object] = ev.cells || {};
      if (s.selBusy) delete s.selBusy[ev.object];
      logAdd(s, `${ev.name}: открыто страниц ${ev.pages} из ${ev.urls}; подтверждено ${(ev.n_document || 0) + (ev.n_official || 0) + (ev.n_secondary || 0)}`
        + `${ev.n_disputed ? `, спорных ${ev.n_disputed}` : ""}${ev.n_unverified ? `, без найденной цитаты ${ev.n_unverified}` : ""}`, ev.n_disputed ? "k-warn" : "");
      return true;
    case "disputes_done":
      logAdd(s, `Спорные ячейки решены: ${ev.cells}`, "k-ok");
      return true;
    case "ranked":
      s.selRank = ev;
      logAdd(s, `Ранг посчитан: первым — ${(ev.names || {})[(ev.ranking[0] || {}).object] || "—"}`, "k-ok");
      return true;
    case "review":
      logAdd(s, `Остановка: ${SEL_GATE_TITLE[ev.gate] || "проверьте список"} — заседание ждёт вас`, "k-warn");
      return true;
    case "review_applied":
      logAdd(s, `Ваше решение по остановке «${SEL_GATE_TITLE[ev.gate] || ev.gate}» применено`, "k-ok");
      return true;
    default:
      return false;
  }
}

/* ---------------------------------------------------------------- живая таблица (пока идёт заполнение) */

function renderSelLive(s) {
  const box = $("selLiveSec");
  if (!box) return;
  const on = s.mode === "selection" && !s.done && (s.selObjs || []).length && (s.selAttrs || []).length;
  box.hidden = !on;
  if (!on) return;
  const attrs = s.selAttrs;
  const cells = s.selCells || {};
  const busy = s.selBusy || {};
  const done = s.selObjs.filter((o) => cells[o.id]).length;
  const rank = {};
  ((s.selRank || {}).ranking || []).forEach((r) => { rank[r.object] = r.rank; });
  const td = (oid, a) => {
    const row = cells[oid];
    if (!row) return `<td class="muted">${busy[oid] ? "…" : ""}</td>`;
    const c = row[a.id] || {};
    const [k, mark, word] = SEL_STATUS[c.status || "none"] || SEL_STATUS.none;
    if (c.status === "disputed") return `<td><span class="badge warn" title="источники расходятся — решит совет">спор</span></td>`;
    if (!c.value) return `<td class="muted">—</td>`;
    const v = `${c.value}${c.unit && !c.value.includes(c.unit) ? ` ${c.unit}` : ""}`;
    return `<td title="${esc(word)}${c.quote ? ` — «${esc(c.quote)}»` : ""}">${esc(v.length > 40 ? v.slice(0, 38) + "…" : v)} <span class="badge ${k}">${mark}</span>${c.ruled ? " ⚖" : ""}</td>`;
  };
  box.innerHTML = `<h2>Таблица заполняется</h2>
    <p class="muted small">Готово ${done} из ${s.selObjs.length}. Д — документ, С — сайт компании, В — вторичный, ? — цитата не найдена,
      «спор» — источники расходятся (решит совет), ⚖ — решено советом. Наведите на ячейку — цитата.</p>
    <div class="table-wrap"><table class="sel-table sel-wide${Object.keys(rank).length ? "" : " sel-live"}"><thead><tr>${Object.keys(rank).length ? "<th>№</th>" : ""}<th>Объект</th>${attrs.map((a) => `<th title="${esc(a.name)}">${esc(selShort(a.name))}</th>`).join("")}</tr></thead><tbody>
    ${[...s.selObjs].sort((x, y) => (rank[x.id] || 999) - (rank[y.id] || 999)).map((o) => `<tr class="${busy[o.id] ? "sel-busy" : ""}">${Object.keys(rank).length ? `<td>${rank[o.id] || ""}</td>` : ""}
      <td><b>${esc(o.name)}</b>${busy[o.id] ? ' <span class="muted small">заполняется…</span>' : !cells[o.id] ? ' <span class="muted small">в очереди</span>' : ""}</td>
      ${attrs.map((a) => td(o.id, a)).join("")}</tr>`).join("")}</tbody></table></div>`;
}

/* ---------------------------------------------------------------- остановки «проверьте список» */

function renderReview(s) {
  const box = $("pReview");
  const p = s.paused;
  const rv = p && p.reason === "review" ? p.review : null;
  box.hidden = !rv;
  $("pResumeRow").hidden = !!rv;
  if (!rv) { box.innerHTML = ""; box.dataset.key = ""; return; }
  const key = `${s.id}|${rv.gate}`;
  if (box.dataset.key === key) return;  // уже нарисовано — не сбивать правки человека
  box.dataset.key = key;
  const pl = rv.payload || {};
  let body = "";
  if (rv.gate === "frame") {
    const fr = pl.frame || {};
    const attrs = fr.attributes || [];
    const li = (xs) => xs.map((x) => `<li>${esc(x)}</li>`).join("");
    body = `${selFrameSummary(fr, li)}
      <details class="sel-help"${innerWidth > 640 ? " open" : ""}><summary>Что значат вес, отсев и тип</summary>
        <dl>
          <dt>Вес 0–10</dt><dd>Насколько признак влияет на место в ранге. Справа от веса — его доля в итоговом балле: признак с весом 6
            влияет вдвое сильнее, чем с весом 3. <b>0</b> — значение соберём и покажем в таблице, но на ранг оно не влияет.
            Веса можно спокойно менять и после заседания ползунками — бесплатно, без моделей.</dd>
          <dt>Отсев</dt><dd>Быстрая дешёвая проверка <b>всех</b> найденных кандидатов по этому признаку ещё до подробной проверки;
            кто не проходит условие — выбывает (его можно вернуть на следующей остановке). Ставьте только на обязательные условия
            («производит сам, а не перепродаёт», «есть лицензия»). Каждый признак отсева удорожает этап отсева.</dd>
          <dt>Тип</dt><dd>Как сравниваем: <b>число</b> — по величине (больше или меньше — лучше); <b>да/нет</b>; <b>дата</b> — позже или раньше;
            <b>список</b> — сколько нужных пунктов есть (формы, сертификаты); <b>текст</b> — только для чтения, на ранг не влияет.</dd>
          <dt>Балл</dt><dd>Для каждого признака объект получает от 0 до 1 (лучший из найденных — 1), это умножается на надёжность
            источника (документ 1, сайт компании 0,8, вторичный 0,6, без цитаты 0,4) и на вес; сумма — балл из 100.</dd>
        </dl></details>
      <p class="muted small">Снимите галочку — признак уберётся совсем.</p>
      <div class="table-wrap"><table class="sel-review"><thead><tr><th class="c-chk" title="Оставить признак">✓</th><th>Признак и как он влияет</th><th class="c-kind">Тип</th><th class="c-w">Вес</th><th class="c-scr" title="Короткая проверка всех кандидатов: не прошёл — выбывает">Отсев</th></tr></thead><tbody>
      ${attrs.map((a) => `<tr data-attr="${esc(a.id)}" data-kind="${esc(a.kind)}"><td class="c-chk"><input type="checkbox" data-keep="${esc(a.id)}" checked aria-label="Оставить «${esc(a.name)}»"></td>
        <td><div><b>${esc(a.name)}</b></div>
          <div class="sel-effect small">${selEffect(a)}</div>
          <div class="muted small"><span class="kind-inline">${esc(SEL_KIND[a.kind] || a.kind)}${a.unit || a.evidence ? " · " : ""}</span>${a.unit ? `в единицах: ${esc(a.unit)}` : ""}${a.evidence ? `<span class="hide-sm">${a.unit ? " · " : ""}доказательство: ${esc(a.evidence)}</span>` : ""}</div>
          ${a.knockout ? `<div class="small sel-ko">Отсев выбивает, если: ${esc(a.knockout)}</div>` : ""}</td>
        <td class="c-kind muted small">${esc(SEL_KIND[a.kind] || a.kind)}</td>
        <td class="c-w">${a.kind === "text" ? '<span class="muted small" title="Текст не сравнивается — на ранг не влияет">—</span>'
          : `<input type="number" min="0" max="10" step="1" value="${Number(a.weight)}" data-w="${esc(a.id)}" class="sel-w" aria-label="Вес «${esc(a.name)}»">`}
          <div class="sel-share muted small" data-share="${esc(a.id)}"></div></td>
        <td class="c-scr"><input type="checkbox" data-screen="${esc(a.id)}" ${a.screen ? "checked" : ""} aria-label="Отсев по «${esc(a.name)}»"></td></tr>`).join("")}</tbody></table></div>
      <label class="field"><span>Добавить признаки (через запятую)</span><input id="rvAddAttr" type="text" placeholder="например: MOQ, срок изготовления"></label>`;
  } else {
    const items = pl.items || [];
    const short = new Set(pl.shortlist || items.map((x) => x.id));
    const comp = pl.completeness || {};
    const sc = { pass: ["ok", "подходит"], fail: ["bad", "не подходит"], unclear: ["plain", "не выяснено"] };
    body = (rv.gate === "candidates" && comp.share != null ? `<p class="small">Полнота списка ≈ <b>${Math.round(comp.share * 100)}%</b> (найдено ${comp.found}, оценка всего ~${comp.estimate}): `
      + (comp.share >= 0.75 ? "разные направления поиска часто находят одних и тех же — список близок к полному." : "многих нашло только одно направление — вероятно, есть ещё; добавьте известных вам.") + "</p>" : "")
      + `<p class="muted small">${rv.gate === "candidates" ? "Снимите галочку у лишних; склеенные дубли отмечены «склеено»." : "Галочка — проверять подробно. Исключённые при отсеве можно вернуть."}
        <button type="button" class="link small" id="rvAll">все</button> · <button type="button" class="link small" id="rvNone">никого</button></p>
      <div class="sel-review-list">${items.map((x) => {
        const scr = x.screen ? sc[x.screen.result] : null;
        return `<label class="sel-item"><input type="checkbox" data-id="${esc(x.id)}" ${rv.gate === "candidates" || short.has(x.id) ? "checked" : ""}>
          <span><b>${esc(x.name)}</b>${(x.names || []).length > 1 ? ` <span class="muted small">${esc(x.names.slice(1, 4).join(", "))}</span>` : ""}
          ${x.merged ? '<span class="badge plain">склеено</span>' : ""}${x.seed ? '<span class="badge plain">ваш список</span>' : ""}
          ${scr ? `<span class="badge ${scr[0]}">${scr[1]}</span>` : ""}
          <small class="muted">${[x.website ? esc(x.website) : "", x.location ? esc(x.location) : "",
            (x.models || []).length ? `нашли ${x.models.length} ${plural(x.models.length, "модель", "модели", "моделей")}: ${esc(x.models.join(", "))}`
              : (x.angles || []).length ? `найден ${x.angles.length} ${plural(x.angles.length, "раз", "раза", "раз")}` : ""].filter(Boolean).join(" · ")}</small>
          ${x.screen && x.screen.reason ? `<small class="muted">${esc(x.screen.reason)}</small>` : x.quote ? `<small class="muted">«${esc(x.quote.slice(0, 160))}»</small>` : ""}</span></label>`;
      }).join("")}</div>
      <label class="field"><span>Добавить своих (по одному в строке)</span><textarea id="rvAdd" rows="3"></textarea></label>`;
  }
  box.innerHTML = `<h3>${esc(SEL_GATE_TITLE[rv.gate] || "Проверьте")}</h3>${body}
    <div class="paused-row"><button type="button" class="primary" id="rvGo">Продолжить с этим</button></div>`;
  const all = (on) => box.querySelectorAll("input[data-id]").forEach((i) => { i.checked = on; });
  if ($("rvAll")) $("rvAll").addEventListener("click", () => all(true));
  if ($("rvNone")) $("rvNone").addEventListener("click", () => all(false));
  $("rvGo").addEventListener("click", () => sendReview(s, rv));
  if (rv.gate === "frame") {
    const upd = () => {
      const rows = [...box.querySelectorAll("tr[data-attr]")];
      const on = (r) => r.querySelector("input[data-keep]").checked;
      const w = (r) => { const i = r.querySelector("input[data-w]"); return i && on(r) ? Math.max(0, Number(i.value) || 0) : 0; };
      const total = rows.reduce((t, r) => t + w(r), 0);
      for (const r of rows) {
        const cell = r.querySelector("[data-share]");
        r.classList.toggle("sel-off", !on(r));
        if (r.dataset.kind === "text") { cell.textContent = "не в ранге"; continue; }
        cell.textContent = !on(r) ? "убран" : w(r) && total ? `${Math.round((w(r) / total) * 100)}% балла` : "не в ранге";
      }
    };
    box.querySelectorAll("input[data-w], input[data-keep]").forEach((i) => i.addEventListener("input", upd));
    upd();
  }
}

// как признак влияет на ранг — простыми словами
const SEL_BETTER = { high: "больше — лучше", low: "меньше — лучше", yes: "лучше, если «да»", no: "лучше, если «нет»",
  newer: "чем позже, тем лучше", older: "чем раньше, тем лучше" };
function selEffect(a) {
  if (a.kind === "text") return '<span class="muted">для чтения: на ранг не влияет</span>';
  const better = a.kind === "list"
    ? `чем больше нужных пунктов, тем лучше${(a.wanted || []).length ? `: ${esc(a.wanted.slice(0, 6).join(", "))}` : ""}`
    : SEL_BETTER[a.better] || (a.kind === "bool" ? SEL_BETTER.yes : a.kind === "date" ? SEL_BETTER.newer : SEL_BETTER.high);
  return `<span class="sel-arrow" aria-hidden="true">↗</span> ${better}${Number(a.weight) ? "" : ' <span class="muted">(вес 0 — сейчас не влияет)</span>'}`;
}

// кого ищем: кто считается кандидатом, кто нет, где ищем
function selFrameSummary(fr, li) {
  const parts = [];
  if ((fr.inclusion || []).length) parts.push(`<div><h4>Кто подходит</h4><ul>${li(fr.inclusion)}</ul></div>`);
  if ((fr.exclusion || []).length) parts.push(`<div><h4>Кто не подходит</h4><ul>${li(fr.exclusion)}</ul></div>`);
  if ((fr.angles || []).length) parts.push(`<div><h4>Где ищем (${fr.angles.length} ${plural(fr.angles.length, "направление", "направления", "направлений")})</h4><ul>${li(fr.angles)}</ul></div>`);
  if (!parts.length) return "";
  const n = [fr.top_n ? `в первую группу — ${fr.top_n}` : "", fr.shortlist ? `подробно проверяем до ${fr.shortlist}` : ""].filter(Boolean).join(" · ");
  return `<details class="sel-help"><summary>Кого ищем ${fr.object_kind ? `(${esc(fr.object_kind)})` : ""}${n ? ` <small>${esc(n)}</small>` : ""}</summary>
    <div class="sel-frame-grid">${parts.join("")}</div></details>`;
}

async function sendReview(s, rv) {
  const box = $("pReview");
  let decision = {};
  if (rv.gate === "frame") {
    const drop = [...box.querySelectorAll("input[data-keep]")].filter((i) => !i.checked).map((i) => i.dataset.keep);
    const weights = Object.fromEntries([...box.querySelectorAll("input[data-w]")].map((i) => [i.dataset.w, Number(i.value) || 0]));
    const screen = [...box.querySelectorAll("input[data-screen]")].filter((i) => i.checked).map((i) => i.dataset.screen);
    const add = ($("rvAddAttr").value || "").split(",").map((x) => x.trim()).filter(Boolean);
    decision = { drop, weights, screen, add };
  } else {
    const boxes = [...box.querySelectorAll("input[data-id]")];
    const add = ($("rvAdd").value || "").split("\n").map((x) => x.trim()).filter(Boolean);
    decision = rv.gate === "candidates"
      ? { drop: boxes.filter((i) => !i.checked).map((i) => i.dataset.id), add }
      : { keep: boxes.filter((i) => i.checked).map((i) => i.dataset.id), add };
    if (rv.gate === "shortlist" && !decision.keep.length && !add.length) { toast("Отметьте хотя бы один объект"); return; }
  }
  $("rvGo").disabled = true;
  try {
    await api(`/api/sessions/${encodeURIComponent(s.id)}/review`, { method: "POST", body: { gate: rv.gate, decision } });
    box.dataset.key = "";
    openSession(s.id);
  } catch (e) { toast(e.message); $("rvGo").disabled = false; }
}

/* ---------------------------------------------------------------- таблица с весами */

const selState = {};  // id заседания → { data, weights, trust }

async function renderSelection(s) {
  const box = $("vSelection");
  const on = s.mode === "selection" && s.done;
  box.hidden = !on;
  if (!on) { box.innerHTML = ""; return; }
  if (box.dataset.for === s.id) return;
  box.dataset.for = s.id;
  box.innerHTML = '<p class="muted small">Загружаю таблицу…</p>';
  await selRank(s.id);
}

let selSeq = 0;  // номер последнего запроса пересчёта: ответы на старые (при быстром движении ползунка) отбрасываются

async function selRank(id, weights, trust) {
  const box = $("vSelection");
  const seq = ++selSeq;
  try {
    const d = await api(`/api/sessions/${encodeURIComponent(id)}/selection/rank`, { method: "POST", body: { weights: weights || null, use_trust: trust ?? null } });
    if (seq !== selSeq) return;
    const reset = !weights;
    selState[id] = { data: d, weights: { ...d.weights }, trust: trust ?? d.use_trust };
    // ползунки рисуются один раз: перерисовка посреди перетаскивания сбивала бы его; «как в заседании» — заново
    if (reset || box.dataset.ctrl !== id) selDraw(id); else selTable(id);
  } catch (e) { if (seq === selSeq) box.innerHTML = `<p class="muted small">Таблица недоступна: ${esc(e.message)}</p>`; box.dataset.ctrl = ""; }
}

function selDraw(id) {
  const st = selState[id];
  const d = st.data;
  const box = $("vSelection");
  const scored = (d.attributes || []).filter((a) => a.scorable_kind);
  box.dataset.ctrl = id;
  box.innerHTML = `<h3>Таблица и веса</h3>
    <p class="muted small">Двигайте веса — ранг пересчитывается сразу, без моделей и без денег. Нажмите на ячейку — источник, цитата и копия страницы.
      Д — документ, С — сайт компании, В — вторичный, ? — цитата не найдена.</p>
    <div class="sel-weights">${scored.map((a) => `<label><span>${esc(a.name)}</span>
      <input type="range" min="0" max="10" step="1" value="${st.weights[a.id] ?? a.weight}" data-wa="${esc(a.id)}"><b>${st.weights[a.id] ?? a.weight}</b></label>`).join("")}
      <label class="switch"><input type="checkbox" id="selTrust" ${st.trust !== false ? "checked" : ""}><span class="sw" aria-hidden="true"></span>
        <span>Учитывать надёжность источника</span></label></div>
    <div class="sel-actions"><a class="btn small" id="selXlsx" href="#"><svg class="ic" aria-hidden="true"><use href="#i-dl"/></svg>Excel с этими весами</a>
      <button type="button" class="btn small" id="selProject"><svg class="ic" aria-hidden="true"><use href="#i-file"/></svg>Создать проект закупки</button>
      <button type="button" class="link small" id="selReset">Веса как в заседании</button></div>
    <div id="selTableBox"></div>
    <div id="selCellBox" class="sel-cellbox" hidden></div>`;
  let timer = null;
  box.querySelectorAll("input[data-wa]").forEach((inp) => inp.addEventListener("input", () => {
    inp.nextElementSibling.textContent = inp.value;
    selState[id].weights[inp.dataset.wa] = Number(inp.value);
    clearTimeout(timer);
    timer = setTimeout(() => selRank(id, selState[id].weights, $("selTrust").checked), 250);
  }));
  $("selTrust").addEventListener("change", () => selRank(id, selState[id].weights, $("selTrust").checked));
  $("selReset").addEventListener("click", () => selRank(id, null, null));
  $("selProject").addEventListener("click", () => createProject(id));
  selTable(id);
}

function selTable(id) {
  const st = selState[id];
  const d = st.data;
  const attrs = d.attributes || [];
  const names = d.names || {};
  const cell = (oid, a) => {
    const c = ((d.cells || {})[oid] || {})[a.id] || {};
    const [k, mark, word] = SEL_STATUS[c.status || "none"] || SEL_STATUS.none;
    if (!c.value) return `<td class="muted">—</td>`;
    const v = `${c.value}${c.unit && !String(c.value).includes(c.unit) ? ` ${c.unit}` : ""}`;
    return `<td><button type="button" class="sel-cell" data-o="${esc(oid)}" data-a="${esc(a.id)}" title="${esc(word)}${c.quote ? ` — «${esc(c.quote.slice(0, 200))}»` : ""}">
      ${esc(v.length > 48 ? v.slice(0, 46) + "…" : v)} <span class="badge ${k}">${mark}</span></button></td>`;
  };
  const top = d.top_n || (d.ranking || []).length;
  const w = st.weights;
  $("selTableBox").innerHTML = `<div class="table-wrap"><table class="sel-table sel-wide"><thead><tr><th>№</th><th>Объект</th><th>Балл</th>${attrs.map((a) => `<th title="${esc(a.name)}">${esc(selShort(a.name))}${a.scorable_kind ? ` <span class="muted small">×${w[a.id] ?? a.weight}</span>` : ""}</th>`).join("")}</tr></thead>
    <tbody>${(d.ranking || []).map((r) => `<tr class="${r.rank <= top ? "sel-top" : ""}"><td>${r.rank}${r.rank <= top ? " ✓" : ""}</td>
      <td><b>${esc(names[r.object] || r.object)}</b></td><td>${String(r.score).replace(".", ",")}</td>${attrs.map((a) => cell(r.object, a)).join("")}</tr>`).join("")}</tbody></table></div>
    ${(d.voi || []).length ? `<h4>Что выяснить, чтобы выбор не поменялся неожиданно</h4><ul>${d.voi.map((v) => `<li>${esc(names[v.object] || v.object)}: ${esc(v.text)}</li>`).join("")}</ul>` : ""}
    ${(d.sensitivity || []).length ? `<h4>Устойчивость ранга к весам</h4><ul>${d.sensitivity.slice(0, 8).map((x) => `<li>${esc(x.text)}</li>`).join("")}</ul>` : ""}`;
  $("selXlsx").href = `/api/sessions/${encodeURIComponent(id)}/selection.xlsx?weights=${encodeURIComponent(Object.entries(w).map(([k, v]) => `${k}:${v}`).join(","))}&trust=${st.trust === false ? 0 : 1}`;
  $("selTableBox").querySelectorAll(".sel-cell").forEach((b) => b.addEventListener("click", () => selCell(id, b.dataset.o, b.dataset.a)));
}

function selCell(id, oid, aid) {
  const d = selState[id].data;
  const c = ((d.cells || {})[oid] || {})[aid] || {};
  const a = (d.attributes || []).find((x) => x.id === aid) || {};
  const [k, , word] = SEL_STATUS[c.status || "none"] || SEL_STATUS.none;
  const opts = (c.options || []).map((o) => `<li>${esc(o.value)} ${esc(o.unit || "")} — ${esc(o.by)}${o.found ? " · цитата найдена" : " · цитата не найдена"}${selUrl(o.url) ? ` · <a href="${esc(selUrl(o.url))}" target="_blank" rel="noopener noreferrer">${esc(o.url.replace(/^https?:\/\//, "").slice(0, 60))}</a>` : ""}</li>`).join("");
  const box = $("selCellBox");
  box.hidden = false;
  box.innerHTML = `<h4>${esc((d.names || {})[oid] || oid)} · ${esc(a.name || aid)}</h4>
    <p><b>${esc(c.value || "—")} ${esc(c.unit || "")}</b> <span class="badge ${k}">${esc(word)}</span>${c.as_of ? ` · на ${esc(c.as_of)}` : ""}${c.ruled ? " · решено советом" : ""}</p>
    ${c.quote ? `<blockquote>«${esc(c.quote)}»</blockquote>` : ""}
    ${c.url ? `<p>${selUrl(c.url) ? `<a href="${esc(selUrl(c.url))}" target="_blank" rel="noopener noreferrer">${esc(c.url)}</a>` : esc(c.url)}${c.copy ? ` · <button type="button" class="link small" id="selCopy">копия страницы</button>` : ""}</p>` : ""}
    ${c.context ? `<p class="muted small">Вокруг цитаты на странице: ${esc(c.context)}</p>` : ""}
    ${opts ? `<details><summary>Варианты источников (${(c.options || []).length})</summary><ul>${opts}</ul></details>` : ""}
    <div id="selCopyText" class="sel-copy" hidden></div>`;
  if ($("selCopy")) $("selCopy").addEventListener("click", async () => {
    try {
      const p = await api(`/api/sources/${encodeURIComponent(c.copy)}`);
      const t = $("selCopyText");
      t.hidden = false;
      t.innerHTML = `<p class="muted small">Копия от ${esc(p.fetched || "—")} · ${esc(p.url)}</p><pre>${esc((p.text || "").slice(0, 20000))}</pre>`;
    } catch (e) { toast(e.message); }
  });
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------------------------------------------------------------- экран вопроса */

function selectionRequest() {
  const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Math.max(0, Number(v) || 0); };
  const review = [["selRevFrame", "frame"], ["selRevCand", "candidates"], ["selRevShort", "shortlist"]]
    .filter(([id]) => $(id).checked).map(([, g]) => g);
  const out = { start: $("selStart").value, review };
  const sl = num("selShortlist"), top = num("selTop");
  if (sl !== null) out.shortlist = sl;
  if (top !== null) out.top_n = top;
  return out;
}

function selectionRestore(o) {
  if (o.start) $("selStart").value = o.start;
  $("selShortlist").value = o.shortlist ?? "";
  $("selTop").value = o.top_n ?? "";
  if (o.review) {
    $("selRevFrame").checked = o.review.includes("frame");
    $("selRevCand").checked = o.review.includes("candidates");
    $("selRevShort").checked = o.review.includes("shortlist");
  }
}
