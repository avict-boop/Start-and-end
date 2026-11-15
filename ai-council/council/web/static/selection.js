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
const SEL_GATE_TITLE = { frame: "Проверьте признаки и веса", candidates: "Проверьте найденных кандидатов",
  shortlist: "Утвердите короткий список" };

function selEvent(s, ev) {
  // события «Отбора» в журнал хода; true — событие обработано
  const n = (x, a, b, c) => `${x} ${plural(x, a, b, c)}`;
  switch (ev.type) {
    case "selection_frame":
      s.selFrame = ev.frame;
      logAdd(s, `Рамка: ${n((ev.frame.attributes || []).length, "признак", "признака", "признаков")}; `
        + (ev.start === "check" ? "проверяем ваш список" : ev.start === "extend" ? "ваш список плюс поиск недостающих" : "ищем кандидатов с нуля")
        + (ev.seeds ? ` (в задании ${n(ev.seeds, "объект", "объекта", "объектов")})` : ""), "k-ok");
      return true;
    case "candidates_found":
      logAdd(s, `Направление «${shortTitle(ev.angle, 70)}»: ${n(ev.count, "кандидат", "кандидата", "кандидатов")}`);
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
      logAdd(s, `Подробно проверяем ${n(ev.count, "объект", "объекта", "объектов")}${ev.excluded ? ` · исключено при отсеве: ${ev.excluded}` : ""}`, "k-ok");
      return true;
    case "object_filled":
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
    const attrs = (pl.frame || {}).attributes || [];
    body = `<p class="muted small">Снимите галочку — признак уберётся; вес 0 — только для сведения; «отсев» — короткая проверка у всех кандидатов.</p>
      <table class="sel-review"><thead><tr><th></th><th>Признак</th><th>Тип</th><th>Вес</th><th>Отсев</th></tr></thead><tbody>
      ${attrs.map((a) => `<tr><td><input type="checkbox" data-keep="${esc(a.id)}" checked></td><td>${esc(a.name)}${a.unit ? ` <span class="muted">(${esc(a.unit)})</span>` : ""}</td>
        <td class="muted small">${esc(a.kind)}</td><td><input type="number" min="0" max="10" step="1" value="${Number(a.weight)}" data-w="${esc(a.id)}" class="sel-w"></td>
        <td><input type="checkbox" data-screen="${esc(a.id)}" ${a.screen ? "checked" : ""}></td></tr>`).join("")}</tbody></table>
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
            (x.angles || []).length ? `нашли направлений: ${x.angles.length}` : ""].filter(Boolean).join(" · ")}</small>
          ${x.screen && x.screen.reason ? `<small class="muted">${esc(x.screen.reason)}</small>` : x.quote ? `<small class="muted">«${esc(x.quote.slice(0, 160))}»</small>` : ""}</span></label>`;
      }).join("")}</div>
      <label class="field"><span>Добавить своих (по одному в строке)</span><textarea id="rvAdd" rows="3"></textarea></label>`;
  }
  box.innerHTML = `<h3>${esc(SEL_GATE_TITLE[rv.gate] || "Проверьте")}</h3>${body}
    <div class="paused-row"><button type="button" class="primary" id="rvGo">▶ Продолжить с этим</button></div>`;
  const all = (on) => box.querySelectorAll("input[data-id]").forEach((i) => { i.checked = on; });
  if ($("rvAll")) $("rvAll").addEventListener("click", () => all(true));
  if ($("rvNone")) $("rvNone").addEventListener("click", () => all(false));
  $("rvGo").addEventListener("click", () => sendReview(s, rv));
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

async function selRank(id, weights, trust) {
  const box = $("vSelection");
  try {
    const d = await api(`/api/sessions/${encodeURIComponent(id)}/selection/rank`, { method: "POST", body: { weights: weights || null, use_trust: trust ?? null } });
    selState[id] = { data: d, weights: { ...d.weights }, trust: trust ?? d.use_trust };
    selDraw(id);
  } catch (e) { box.innerHTML = `<p class="muted small">Таблица недоступна: ${esc(e.message)}</p>`; }
}

function selDraw(id) {
  const st = selState[id];
  const d = st.data;
  const box = $("vSelection");
  const attrs = d.attributes || [];
  const scored = attrs.filter((a) => a.scorable_kind);
  const names = d.names || {};
  const cell = (oid, a) => {
    const c = ((d.cells || {})[oid] || {})[a.id] || {};
    const [k, mark, word] = SEL_STATUS[c.status || "none"] || SEL_STATUS.none;
    if (!c.value) return `<td class="muted">—</td>`;
    const v = `${c.value}${c.unit && !String(c.value).includes(c.unit) ? ` ${c.unit}` : ""}`;
    return `<td><button type="button" class="sel-cell" data-o="${esc(oid)}" data-a="${esc(a.id)}" title="${esc(word)}${c.quote ? ` — «${c.quote.slice(0, 200)}»` : ""}">
      ${esc(v.length > 48 ? v.slice(0, 46) + "…" : v)} <span class="badge ${k}">${mark}</span></button></td>`;
  };
  const top = d.top_n || (d.ranking || []).length;
  box.innerHTML = `<h3>Таблица и веса</h3>
    <p class="muted small">Двигайте веса — ранг пересчитывается сразу, без моделей и без денег. Нажмите на ячейку — источник, цитата и копия страницы.
      Д — документ, С — сайт компании, В — вторичный, ? — цитата не найдена.</p>
    <div class="sel-weights">${scored.map((a) => `<label><span>${esc(a.name)}</span>
      <input type="range" min="0" max="10" step="1" value="${st.weights[a.id] ?? a.weight}" data-wa="${esc(a.id)}"><b>${st.weights[a.id] ?? a.weight}</b></label>`).join("")}
      <label class="switch"><input type="checkbox" id="selTrust" ${st.trust !== false ? "checked" : ""}><span class="sw" aria-hidden="true"></span>
        <span>Учитывать надёжность источника</span></label></div>
    <div class="sel-actions"><a class="btn small" id="selXlsx" href="#">⬇ Excel с этими весами</a>
      <button type="button" class="btn small" id="selProject">📦 Создать проект закупки</button>
      <button type="button" class="link small" id="selReset">Веса как в заседании</button></div>
    <div class="table-wrap"><table class="sel-table"><thead><tr><th>№</th><th>Объект</th><th>Балл</th>${attrs.map((a) => `<th>${esc(a.name)}${a.scorable_kind ? ` <span class="muted small">×${st.weights[a.id] ?? a.weight}</span>` : ""}</th>`).join("")}</tr></thead>
    <tbody>${(d.ranking || []).map((r) => `<tr class="${r.rank <= top ? "sel-top" : ""}"><td>${r.rank}${r.rank <= top ? " ✓" : ""}</td>
      <td><b>${esc(names[r.object] || r.object)}</b></td><td>${String(r.score).replace(".", ",")}</td>${attrs.map((a) => cell(r.object, a)).join("")}</tr>`).join("")}</tbody></table></div>
    ${(d.voi || []).length ? `<h4>Что выяснить, чтобы выбор не поменялся неожиданно</h4><ul>${d.voi.map((v) => `<li>${esc(names[v.object] || v.object)}: ${esc(v.text)}</li>`).join("")}</ul>` : ""}
    ${(d.sensitivity || []).length ? `<h4>Устойчивость ранга к весам</h4><ul>${d.sensitivity.slice(0, 8).map((x) => `<li>${esc(x.text)}</li>`).join("")}</ul>` : ""}
    <div id="selCellBox" class="sel-cellbox" hidden></div>`;
  let timer = null;
  box.querySelectorAll("input[data-wa]").forEach((inp) => inp.addEventListener("input", () => {
    inp.nextElementSibling.textContent = inp.value;
    st.weights[inp.dataset.wa] = Number(inp.value);
    clearTimeout(timer);
    timer = setTimeout(() => selRank(id, st.weights, $("selTrust").checked), 250);
  }));
  $("selTrust").addEventListener("change", () => selRank(id, st.weights, $("selTrust").checked));
  $("selReset").addEventListener("click", () => selRank(id, null, null));
  $("selProject").addEventListener("click", () => createProject(id));
  $("selXlsx").href = `/api/sessions/${encodeURIComponent(id)}/selection.xlsx?weights=${encodeURIComponent(Object.entries(st.weights).map(([k, v]) => `${k}:${v}`).join(","))}`;
  box.querySelectorAll(".sel-cell").forEach((b) => b.addEventListener("click", () => selCell(id, b.dataset.o, b.dataset.a)));
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
