"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x) => (x == null ? "—" : `$${Number(x).toFixed(4)}`);

const STAGES = [
  ["framing", "Уточняем задачу"],
  ["proposal", "Варианты ответа"],
  ["critique", "Ищем слабые места"],
  ["revision", "Доработка"],
  ["vote", "Голосование"],
  ["verdict", "Готовим вывод"],
];
const ARB_STAGE = ["arbitration", "Проверяем утверждения по источникам"];
// проверка ссылок реестра: открывается ли страница
const LINK_BADGE = { dead: ' <span class="badge bad">ссылка не открывается</span>', blocked: ' <span class="badge plain" title="Сайт не пускает автоматическую проверку — сама ссылка может быть верной">сайт не пускает проверку</span>',
  error: ' <span class="badge plain">не открылась при проверке</span>',
  refused: ' <span class="badge bad" title="Ссылка ведёт внутрь сети сервера — такие адреса не открываются">внутренний адрес</span>' };
const STAGE_NAME = { ...Object.fromEntries(STAGES), describe: "Описание изображений", attachments: "Вложения",
  factcheck: "Проверка фактов", arbitration: "Проверяем утверждения по источникам", recheck: "Проверяем новые утверждения" };
// что происходит на этапе — простыми словами
const STAGE_HINT = {
  framing: "председатель уточняет вопрос и критерии",
  proposal: "каждая модель вслепую предлагает решение",
  critique: "модели анонимно оценивают чужие решения",
  revision: "авторы улучшают решения с учётом критики",
  vote: "модели ранжируют чужие решения",
  factcheck: "модель с поиском проверяет факты лидеров",
  arbitration: "поиск проверяет утверждения всех вариантов: подтверждения и опровержения",
  recheck: "поиск проверяет утверждения, которые авторы добавили после доработки",
  verdict: "председатель пишет итоговое решение",
};
// исследование: этапы те же по смыслу, но работа на них другая — свои названия и пояснения
const RESEARCH_STAGE = {
  framing: ["Уточняем задачу", "председатель задаёт рамки, подвопросы и углы зрения авторов"],
  proposal: ["Варианты ответа", "каждый автор со своего угла зрения пишет вариант с проверяемыми утверждениями"],
  critique: ["Ищем слабые места", "модели оценивают чужие варианты, отмечают спорные утверждения и пропущенные подвопросы"],
  revision: ["Доработка", "авторы дорабатывают варианты по найденным доказательствам и выжимкам чужих вариантов"],
  vote: ["Голосование", "модели ранжируют варианты и выбирают лучший по каждому подвопросу"],
  verdict: ["Готовим вывод", "председатель собирает итог: известно, спорно, неизвестно — с источниками"],
};
const stageHint = (s, key) => (s.mode === "research" && RESEARCH_STAGE[key] ? RESEARCH_STAGE[key][1] : STAGE_HINT[key]) || "";
const MODE_HINT = {
  debate: "Подробное обсуждение: решения, критика, доработка, голосование.",
  quick: "Быстрый ответ: решения и голосование, без критики.",
  evaluate: "Выбор из ваших вариантов: совет их обсуждает и выбирает лучший.",
  research: "Исследование: подвопросы и углы зрения, проверка утверждений всех вариантов поиском, итог «известно / спорно / неизвестно» с источниками.",
};
const MODE_NAME = { debate: "Подробное обсуждение", quick: "Быстрый ответ", evaluate: "Выбор из вариантов", research: "Исследование" };

/* ------------------------------------------------------------ мелкие помощники интерфейса */

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

let toastTimer = null;
function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// своё окно подтверждения вместо системного confirm(): крупные кнопки, понятный текст, работает с клавиатуры
function ask({ title, html = "", ok = "OK", cancel = "Отмена", danger = false, wide = false }) {
  return new Promise((resolve) => {
    const d = $("dialog");
    d.querySelector(".dialog-box").classList.toggle("wide", wide);
    const back = document.activeElement;
    $("dTitle").textContent = title;
    $("dBody").innerHTML = html;
    $("dOk").textContent = ok;
    $("dOk").className = `btn ${danger ? "danger" : "primary"}`;
    $("dCancel").textContent = cancel;
    $("dCancel").hidden = cancel === null;
    d.hidden = false;
    document.body.classList.add("modal-open");
    const done = (v) => {
      d.hidden = true;
      if ($("modal").hidden) document.body.classList.remove("modal-open");
      d.removeEventListener("keydown", onKey);
      $("dOk").onclick = $("dCancel").onclick = d.onclick = null;
      if (back && back.focus) back.focus();
      resolve(v);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } trapFocus(e, d); };
    d.addEventListener("keydown", onKey);
    $("dOk").onclick = () => done(true);
    $("dCancel").onclick = () => done(false);
    d.onclick = (e) => { if (e.target === d) done(false); };
    (danger ? $("dCancel") : $("dOk")).focus();
  });
}

// Tab не выходит за пределы открытого окна
function trapFocus(e, box) {
  if (e.key !== "Tab") return;
  const items = [...box.querySelectorAll('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((el) => !el.disabled && !el.hidden && el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ------------------------------------------------------------ markdown (минимальный, безопасный) */

// длинный адрес показываем коротко: домен и конец пути; полный — в подсказке и по нажатию
function shortUrl(u) {
  try {
    const x = new URL(u);
    const host = x.hostname.replace(/^www\./, "");
    const parts = x.pathname.split("/").filter(Boolean);
    let path = parts.length ? `/${parts.join("/")}` : "";
    if ((host + path).length > 48) {
      const last = parts[parts.length - 1] || "";
      path = parts.length > 1 ? `/…/${last.length > 24 ? `${last.slice(0, 22)}…` : last}` : `/${last.slice(0, 30)}…`;
    }
    return host + path + (x.search || x.hash ? (path.endsWith("…") ? "" : "…") : "");
  } catch { return u.length > 50 ? `${u.slice(0, 48)}…` : u; }
}
function linkHtml(url, text) {
  return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(url)}">${esc(text)}</a>`;
}

function inline(s) {
  // код и ссылки откладываем, чтобы разметка не задела адреса (в них бывают * и _)
  const keep = [];
  const hold = (html) => { keep.push(html); return `\u0001${keep.length - 1}\u0001`; };
  let t = String(s ?? "")
    .replace(/`([^`]+)`/g, (_, c) => hold(`<code>${esc(c)}</code>`))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, txt, u) => hold(linkHtml(u, /^https?:\/\/\S+$/.test(txt.trim()) ? shortUrl(txt.trim()) : txt)))
    .replace(/https?:\/\/[^\s<>()"'`«»]+[^\s<>()"'`«».,;:!?]/g, (u) => hold(linkHtml(u, shortUrl(u))));
  t = esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>");
  return t.replace(/\u0001(\d+)\u0001/g, (_, i) => keep[+i]);
}

function md(src) {
  const code = [];
  let text = String(src ?? "").replace(/\r\n?/g, "\n");
  text = text.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, c) => { code.push(c); return `\u0000${code.length - 1}\u0000`; });
  const lines = text.split("\n");
  const out = [];
  const listRe = /^\s*([-*+•]|\d+[.)])\s+(.*)$/;
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = line.match(/^\u0000(\d+)\u0000$/))) { out.push(`<pre><code>${esc(code[+m[1]])}</code></pre>`); i++; continue; }
    if ((m = line.match(/^#{1,6}\s+(.*)$/))) { out.push(`<h3 class="mdh">${inline(m[1])}</h3>`); i++; continue; }
    if (/^\s*\|/.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || "")) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      // широкая таблица прокручивается внутри своего блока и не раздвигает карточку/окно
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (listRe.test(line)) {
      // собираем подряд идущие пункты (с отступами — вложенные) и строим дерево
      const items = [];
      while (i < lines.length && listRe.test(lines[i])) {
        const m = lines[i++].match(/^(\s*)([-*+•]|\d+[.)])\s+(.*)$/);
        items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), text: m[3] });
      }
      const steps = items.map((x) => x.indent).filter((n) => n > 0);
      const unit = steps.length ? Math.min(...steps) : 4;
      for (const it of items) it.level = Math.round(it.indent / unit);
      const build = (from, level) => {
        let html = "", j = from, tag = null;
        while (j < items.length && items[j].level >= level) {
          const it = items[j];
          if (it.level > level) { j = build(j, level + 1).next; continue; }
          const t = it.ordered ? "ol" : "ul";
          if (tag !== t) { if (tag) html += `</${tag}>`; html += `<${t}>`; tag = t; }
          html += `<li>${inline(it.text)}`;
          j++;
          if (j < items.length && items[j].level > level) { const sub = build(j, level + 1); html += sub.html; j = sub.next; }
          html += "</li>";
        }
        if (tag) html += `</${tag}>`;
        return { html, next: j };
      };
      out.push(build(0, 0).html);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${q.map(inline).join("<br>")}</blockquote>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !listRe.test(lines[i]) && !/^(#{1,6}\s|>|\u0000)/.test(lines[i])
           && !/^\s*\|/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) { out.push(`<p>${inline(lines[i++])}</p>`); continue; }
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}

/* ------------------------------------------------------------ API */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* пустой ответ */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("\n") : d;
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data;
}

/* ------------------------------------------------------------ форма */

let config = null;
let mode = "debate";

function setMode(m) {
  mode = m;
  if ($("depthField")) $("depthField").hidden = m !== "research";
  if ($("freshField")) $("freshField").hidden = m === "evaluate";
  for (const r of $("modeSeg").querySelectorAll("input[name=mode]")) r.checked = r.value === m;
  $("roundsField").hidden = m !== "debate";
  $("optionsField").hidden = m !== "evaluate";
  // у исследования своя длина варианта (Настройки → Исследование) — «компактные варианты» к нему не относятся
  $("compact").closest("label").hidden = m === "research";
  $("researchLen").hidden = m !== "research";
  $("factCheckRow").hidden = m === "research";  // в исследовании вместо неё — арбитраж доказательств
  if ($("webField") && config) renderWebMembers();
  $("modeHint").textContent = MODE_HINT[m];
  if ($("chairHint")) updateChairHint();
  formChanged();
}

// порог «слишком длинного» варианта — от длины, заданной для этого вопроса
function updateShortenHint() {
  if (!config || !config.research) return;
  // без ограничения длины поле длины и «сокращать» не нужны — прячем их
  $("researchWords").closest("label").hidden = noLimit();
  $("researchShorten").closest("label").hidden = noLimit();
  if (noLimit()) return;
  const w = Number($("researchWords").value) || config.research.words || 1400, f = config.research.factor;
  $("researchShortenHint").textContent = `Вариант длиннее ${Math.round(w * f)} слов (в ${String(f).replace(".", ",")} раза `
    + "больше заданного) вернётся автору на сокращение. Председателю дешевле читать, но иногда это лишний запрос.";
}

// состав на этот вопрос (по «Подобрать состав» или вручную): другая модель у участника, новые участники, поиск
let lineup = { models: {}, extra: [], web: new Set() };
// другие настройки исследования на этот вопрос (из рекомендации советника): {ключ: значение}
let researchTuned = {};
const RS_NAME = {
  arbitration_claims: "Утверждений на проверку поиском", missions: "Поисковых заданий (подтвердить / опровергнуть)",
  fresh_scouts: "Авторов с поиском уже в предложениях", max_words: "Длина варианта, слов", shorten_long: "Сокращать слишком длинные варианты",
  max_claims: "Утверждений в варианте", lenses: "Углы зрения авторов", section_vote: "Голосование по подвопросам",
  revision_digest: "Доработка по выжимкам", long_factor: "«Слишком длинный» — во сколько раз", arbitration: "Арбитраж доказательств",
  link_check: "Проверка ссылок",
};
const rsVal = (v) => (v === true ? "да" : v === false ? "нет" : String(v));

function renderResearchTuned() {
  const keys = Object.keys(researchTuned).filter((k) => !["max_words", "shorten_long"].includes(k));
  const box = $("researchTuned");
  box.hidden = !keys.length;
  if (!keys.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<b>Для этого вопроса по рекомендации:</b> ${keys.map((k) => `${esc(RS_NAME[k] || k)} — <b>${esc(rsVal(researchTuned[k]))}</b>`).join("; ")}.
    <button type="button" class="link small" id="researchTunedReset">Вернуть из настроек</button>`;
  $("researchTunedReset").addEventListener("click", () => { researchTuned = {}; renderResearchTuned(); updateParamsSummary(); formChanged(); });
}

function memberList() {
  return [...config.members, ...lineup.extra.map((x) => ({ ...x, enabled: true, extra: true }))];
}

function renderMembers(checked) {
  const sel = checked || new Set(config.members.filter((m) => m.enabled).map((m) => m.name));
  $("members").innerHTML = memberList().map((m) => {
    const model = lineup.models[m.name] || m.model;
    const changed = !!lineup.models[m.name] && lineup.models[m.name] !== m.model;
    const mark = m.extra ? "новый участник — только для этого вопроса" : changed ? `подобрано для этого вопроса (в настройках: ${m.model})` : "";
    return `<div class="member-cell"><label class="member${mark ? " picked" : ""}" title="${esc(model)}${mark ? ` — ${esc(mark)}` : ""}"><input type="checkbox" value="${esc(m.name)}" ${sel.has(m.name) ? "checked" : ""}>
      <span><b>${esc(m.name)}</b><code>${mark ? "✦ " : ""}${esc(model.split("/").pop())}</code></span></label>
      <button type="button" class="m-pick" data-pick-model="${esc(m.name)}" title="Выбрать модель из линейки ${esc(model.split("/")[0])} — для этого вопроса" aria-label="Другая модель для ${esc(m.name)}">▾</button></div>`;
  }).join("");
  renderWebMembers();
}

// кнопка «Модели по умолчанию» — видна, когда состав отличается от настроек
function updateResetBtn() {
  if (!config) return;
  const def = config.members.filter((m) => m.enabled).map((m) => m.name).sort().join();
  const defWeb = config.members.filter((m) => m.enabled && m.web_search).map((m) => m.name).sort().join();
  const changed = Object.keys(lineup.models).length || lineup.extra.length
    || selectedMembers().sort().join() !== def || [...lineup.web].sort().join() !== defWeb
    || ($("chair").value && $("chair").value !== config.chair) || ($("devil").value || "") !== (config.devil_advocate || "");
  $("lineupReset").hidden = !changed;
}

// другая модель того же разработчика для участника — только на этот вопрос (с описаниями моделей)
async function pickMemberModel(name) {
  const m = memberList().find((x) => x.name === name);
  if (!m) return;
  const cur = lineup.models[name] || m.model;
  const chosen = await chooseFamilyModel(cur, `Модель для «${name}» — на этот вопрос`,
    "Выбор действует только для этого вопроса; постоянно — «Настройки → Модели».");
  if (!chosen) return;
  const sel = new Set(selectedMembers());
  sel.add(name);
  const extra = lineup.extra.find((x) => x.name === name);
  if (extra) extra.model = chosen;
  else if (chosen === m.model) delete lineup.models[name];
  else lineup.models[name] = chosen;
  renderMembers(sel);
  refreshRoleSelects();
  formChanged();
  toast(`${name}: ${chosen.split("/").pop()} — для этого вопроса`);
}

// сбросить только модели и роли (режим, вопрос и остальные параметры не трогаем)
function resetModels() {
  lineup = { models: {}, extra: [], web: new Set(config.members.filter((m) => m.enabled && m.web_search).map((m) => m.name)) };
  renderMembers();
  $("chair").value = "";
  $("devil").value = "";
  refreshRoleSelects();
  formChanged();
}

function renderWebMembers() {
  const names = selectedMembers();
  for (const n of [...lineup.web]) if (!names.includes(n)) lineup.web.delete(n);
  $("webField").hidden = mode === "evaluate" || !names.length;
  $("webMembers").innerHTML = names.map((n) => `<button type="button" class="chip-tg${lineup.web.has(n) ? " on" : ""}" data-web="${esc(n)}" aria-pressed="${lineup.web.has(n)}">🌐 ${esc(n)}</button>`).join("");
  syncFresh();
  updateResetBtn();
}

function selectedMembers() {
  return [...$("members").querySelectorAll("input:checked")].map((i) => i.value);
}

// строка под «Параметрами»: что будет использовано, без открытия списка
function updateParamsSummary() {
  if (!config) return;
  const n = selectedMembers().length;
  const custom = Object.keys(lineup.models).length || lineup.extra.length
    || config.members.some((m) => m.enabled !== selectedMembers().includes(m.name));
  const bits = [`${custom ? "Ваш состав" : "Готовый состав"} · ${n} ${plural(n, "модель", "модели", "моделей")}`];
  if ($("chair").value) bits.push(`председатель ${$("chair").value}`);
  if (mode === "debate") { const r = Number($("rounds").value) || 1; bits.push(`${r} ${plural(r, "раунд", "раунда", "раундов")}`); }
  bits.push(`бюджет $${Number($("budget").value) || config.budget}`);
  if ($("compact").checked && mode !== "research") bits.push("компактно");
  if (mode === "research") bits.push(noLimit() ? "вариант без ограничения длины"
    : `вариант до ${Number($("researchWords").value) || "—"} слов${$("researchShorten").checked ? ", длинные — на сокращение" : ""}`);
  if (mode === "research" && researchTuned.arbitration_claims) bits.push(`проверка ${researchTuned.arbitration_claims} утв.`);
  if ($("factCheck").checked && mode !== "research") bits.push("проверка фактов");
  const web = [...lineup.web].filter((x) => selectedMembers().includes(x));
  if (web.length && mode !== "evaluate") bits.push(`поиск: ${web.join(", ")}`);
  if ($("mock").checked) bits.push("демо");
  if ($("answerSize").value === "brief") bits.push("краткий итог");
  $("paramsSummary").textContent = bits.join(" · ");
}

/* ------------------------------------------------------------ оценка стоимости — сама, по мере ввода */

let estTimer = null, estSeq = 0, lastEstimate = null;
function formChanged() {
  if (!config) return;
  updateParamsSummary();
  lastEstimate = null;
  clearTimeout(estTimer);
  if ($("question").value.trim().length >= 3) { $("estLine").textContent = "Оцениваем стоимость…"; $("estimateBtn").className = "est-line"; }
  estTimer = setTimeout(autoEstimate, 700);
}

async function autoEstimate() {
  const p = payload();
  const line = $("estLine");
  $("estimateBtn").className = "est-line";
  if (p.question.length < 3) { line.textContent = "Напишите вопрос — оценим стоимость"; $("estimate").hidden = true; return; }
  if (p.members.length < 2) { line.textContent = "Выберите хотя бы двух участников"; $("estimateBtn").classList.add("warn"); return; }
  const seq = ++estSeq;
  line.textContent = "Считаю стоимость…";
  let e;
  try { e = await api("/api/estimate", { method: "POST", body: p }); } catch (err) {
    if (seq === estSeq) { line.textContent = `Не удалось оценить: ${err.message}`; $("estimateBtn").classList.add("warn"); }
    return;
  }
  if (seq !== estSeq) return;
  lastEstimate = e;
  renderEstimate(e);
}

function estimateSummary(e) {
  if ((Object.keys(e.unknown || {})).length) return ["Есть модели, которых нет на OpenRouter — нажмите, чтобы посмотреть", "bad"];
  if (e.low == null) return [$("mock").checked ? `Демо-режим: бесплатно · ${e.calls} обращений к моделям` : `${e.calls} обращений · цены неизвестны`, ""];
  let txt = `Оценка: $${e.low.toFixed(2)} – $${e.high.toFixed(2)} · бюджет $${e.budget}`;
  let kind = "";
  const kl = e.key_limit;
  if (e.balance != null && e.balance < e.high) { txt += " · баланса может не хватить"; kind = "bad"; }
  else if (kl && kl.remaining < e.high) { txt += ` · лимита ключа может не хватить (осталось $${kl.remaining.toFixed(2)})`; kind = "bad"; }
  else if (e.high > e.budget) { txt += ` · выше лимита $${e.budget}`; kind = "warn"; }
  else if (e.balance != null) txt += " · баланса хватит";
  return [txt, kind];
}

function refreshRoleSelects() {
  const names = selectedMembers();
  const fill = (sel, withNone, prefer) => {
    const cur = sel.value || prefer;
    sel.innerHTML = (withNone ? '<option value="">— нет —</option>' : "") +
      names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (names.includes(cur)) sel.value = cur;
    else if (!withNone && names.length) sel.value = names[0];
    else if (withNone) sel.value = "";
  };
  const before = $("chair").value;
  fill($("chair"), false, config.chair);
  fill($("devil"), true, config.devil_advocate);
  if (before && before !== $("chair").value && !names.includes(before)) toast(`Председатель теперь ${$("chair").value}: ${before} не выбран`);
  updateChairHint();
  updateResetBtn();
}

// сколько будет вариантов: председатель обычно своего не предлагает (если без него хватает участников для кворума)
function updateChairHint() {
  const names = selectedMembers();
  const chair = $("chair").value;
  const judge = !$("chairProposes").checked && mode !== "evaluate" && names.length - 1 >= 2;
  const n = judge ? names.length - 1 : names.length;
  $("chairHint").textContent = !chair || mode === "evaluate" ? "" : judge
    ? `${chair} — председатель: ставит задачу, оценивает чужие варианты и пишет итог, своего не предлагает. Вариантов будет ${n} из ${names.length} моделей.`
    : `${chair} — председатель и тоже предлагает свой вариант. Вариантов будет ${n}.`;
}

// переключатели — по умолчаниям из config.yaml (только при загрузке, чтобы смена участников их не сбрасывала)
function resetToggles() {
  $("chairProposes").checked = !!config.chair_proposes;
  $("factCheck").checked = !!(config.fact_check && config.fact_check.enabled);
  $("compact").checked = !!(config.compact && config.compact.enabled);
  if (config.research) {
    $("researchNoLimit").checked = !config.research.words;
    $("researchWords").value = config.research.words || 1400;
    $("researchShorten").checked = !!config.research.shorten;
    updateShortenHint();
    updateShortenHint();
  }
  $("compactHint").textContent = config.compact ? `До ${config.compact.words} слов на вариант.` : "";
  $("factCheckHint").textContent = config.fact_check ? `Модель: ${config.fact_check.model}, лидеров: ${config.fact_check.top}. Дороже на пару запросов.` : "";
}

function parseOptions(text) {
  const t = text.replace(/\r\n?/g, "\n").trim();
  if (!t) return [];
  const parts = /\n\s*---+\s*\n/.test(t) ? t.split(/\n\s*---+\s*\n/) : t.split("\n");
  return parts.map((s) => s.trim()).filter(Boolean);
}

function payload() {
  return {
    question: $("question").value.trim(),
    mode,
    rounds: mode === "debate" ? Number($("rounds").value) || 1 : null,
    criteria: $("criteria").value.split(",").map((s) => s.trim()).filter(Boolean),
    options: mode === "evaluate" ? parseOptions($("options").value) : null,
    context: $("context").value.trim() || null,
    members: selectedMembers(),
    models: Object.keys(lineup.models).length ? lineup.models : null,
    extra_members: lineup.extra.length ? lineup.extra : null,
    web_search: mode === "evaluate" ? [] : [...lineup.web].filter((n) => selectedMembers().includes(n)),
    chair: $("chair").value || null,
    devil_advocate: $("devil").value,
    chair_proposes: $("chairProposes").checked,
    fact_check: $("factCheck").checked,
    compact: mode === "research" ? false : $("compact").checked,
    research_words: mode === "research" ? (noLimit() ? 0 : Number($("researchWords").value) || null) : null,
    research_shorten: mode === "research" ? $("researchShorten").checked : null,
    research: mode === "research" && Object.keys(researchTuned).length
      ? Object.fromEntries(Object.entries(researchTuned).filter(([k]) => !["max_words", "shorten_long"].includes(k))) : null,
    budget: Number($("budget").value) || null,
    answer_size: $("answerSize").value,
    mock: $("mock").checked,
    attachments: uploads.filter((u) => u.status === "ready").map((u) => u.meta.id),
    parent: formMode && formMode.kind === "followup" ? formMode.id : null,
  };
}

/* ------------------------------------------------------------ уточнение и повтор */

// состав и роли из запроса прошлого заседания: при повторе и уточнении по умолчанию — те же модели
function applyLineup(r) {
  if (r.members && r.members.length) {
    const base = Object.fromEntries(config.members.map((m) => [m.name, m.model]));
    lineup = {
      models: Object.fromEntries(Object.entries(r.models || {}).filter(([n, mdl]) => base[n] && base[n] !== mdl)),
      extra: (r.extra_members || []).filter((x) => !base[x.name]),
      web: new Set(r.web_search || []),
    };
    renderMembers(new Set(r.members.filter((n) => base[n] || lineup.extra.some((x) => x.name === n))));
    refreshRoleSelects();
  }
  if (r.chair && selectedMembers().includes(r.chair)) $("chair").value = r.chair;
  if (r.devil_advocate !== undefined) $("devil").value = selectedMembers().includes(r.devil_advocate) ? r.devil_advocate : "";
  if (r.chair_proposes !== undefined) $("chairProposes").checked = !!r.chair_proposes;
  if (r.fact_check !== undefined) $("factCheck").checked = !!r.fact_check;
  if (r.answer_size) $("answerSize").value = r.answer_size;
  if (r.compact_proposals !== undefined) $("compact").checked = !!r.compact_proposals;
  if (r.research_words !== undefined && r.research_words !== null) {
    $("researchNoLimit").checked = r.research_words === 0;
    if (r.research_words) $("researchWords").value = r.research_words;
    updateShortenHint();
  }
  if (r.research_shorten !== undefined) $("researchShorten").checked = !!r.research_shorten;
  researchTuned = { ...(r.research_overrides || {}) };
  renderResearchTuned();
  updateChairHint();
}

// новая задача (не уточнение и не повтор) — состав из настроек; изменить его можно «Подобрать состав»
function resetLineup() {
  if (!config) return;
  lineup = { models: {}, extra: [], web: new Set(config.members.filter((m) => m.enabled && m.web_search).map((m) => m.name)) };
  renderMembers();
  $("chair").value = "";
  $("devil").value = "";
  refreshRoleSelects();
  resetToggles();
  $("budget").value = config.budget;
  researchTuned = {};
  renderResearchTuned();
  $("adviceBox").hidden = true;
  setMode(config.mode === "evaluate" ? "debate" : config.mode);
}

let formMode = null;  // {kind: "followup" | "repeat", id, title}

function setFormMode(mode) {
  formMode = mode;
  $("formBanner").hidden = !mode;
  if (mode) {
    $("formBannerText").innerHTML = mode.kind === "followup"
      ? `↳ <b>Уточнение</b> к заседанию «${esc(mode.title)}». Совет учтёт его решение и вложения. Модели — те же, что в нём; поменять можно в «Параметрах» или «Подобрать состав».`
      : `🔁 <b>Повтор</b> заседания «${esc(mode.title)}». Модели — те же, что в нём; можно поменять модели, режим или вопрос.`;
  }
}
$("formBannerX").addEventListener("click", () => { setFormMode(null); resetLineup(); });

function focusForm() {
  goCompose();
  window.scrollTo({ top: 0 });
  setTimeout(() => $("question").focus(), 100);
  formChanged();
}

$("followBtn").addEventListener("click", async () => {
  if (!current) return;
  const sid = current.id;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(sid)}/request`);
    if (r.mode && r.mode !== "evaluate") setMode(r.mode);
    applyLineup(r);
  } catch (e) { toast(`Состав исходного заседания не загружен: ${e.message}`); }
  setFormMode({ kind: "followup", id: sid, title: shortTitle(current ? current.question : "") });
  $("question").value = "";
  $("question").placeholder = "Например: а если бюджет вдвое меньше? Раскрой подробнее вариант B";
  focusForm();
});

$("repeatBtn").addEventListener("click", async () => {
  if (!current) return;
  let data;
  try { data = await api(`/api/sessions/${encodeURIComponent(current.id)}/repeat`, { method: "POST" }); }
  catch (e) { toast(e.message); return; }
  const r = data.request || {};
  $("question").value = r.question || "";
  setMode(["debate", "quick", "evaluate", "research"].includes(r.mode) ? r.mode : "debate");
  if (r.rounds) $("rounds").value = r.rounds;
  $("criteria").value = (r.criteria || []).join(", ");
  $("options").value = (r.options || []).join("\n---\n");
  $("context").value = r.context || "";
  applyLineup(r);
  if (r.budget) $("budget").value = r.budget;
  uploads = (data.uploads || []).map((m) => ({ key: ++uploadSeq, name: m.name, status: "ready", meta: m }));
  renderUploads();
  setFormMode({ kind: "repeat", id: current.id, title: data.title });
  $("params").open = true;  // при повторе обычно меняют модели или режим
  focusForm();
});

/* ------------------------------------------------------------ вложения в форме */

let uploads = [];          // {key, name, status: uploading|ready|error, meta, error}
let uploadCfg = { accept: [], max_files: 10, max_mb: 20 };
let uploadSeq = 0;

function extIcon(name) { return (String(name).split(".").pop() || "файл").slice(0, 4).toUpperCase(); }

function renderUploads() {
  $("attList").innerHTML = uploads.map((u) => {
    const m = u.meta || {};
    const ico = m.thumb ? `<img src="${m.thumb}" alt="">` : u.status === "uploading" ? "" : esc(extIcon(u.name));
    let sub = "";
    if (u.status === "uploading") sub = "загружаю и читаю…";
    else if (u.status === "error") sub = u.error;
    else {
      const bits = [];
      if (m.kind === "image") bits.push(`картинка ${m.width}×${m.height}`);
      if (m.pages) bits.push(`${m.pages} стр.`);
      if (m.kind === "document" && m.chars) bits.push(`${m.chars.toLocaleString("ru-RU")} симв.${m.truncated ? " (обрезан)" : ""}`);
      bits.push(m.label ? m.label.split(" — ").pop() : "");
      sub = bits.filter(Boolean).join(" · ");
    }
    const warn = m.warning ? `<div class="sub warn">⚠ ${esc(m.warning)}</div>` : "";
    return `<li class="att ${u.status}"><div class="ico">${ico}</div>
      <div style="min-width:0"><div class="nm" title="${esc(u.name)}">${esc(u.name)}</div><div class="sub">${esc(sub)}</div>${warn}</div>
      <button type="button" class="rm" data-rm="${u.key}" aria-label="Убрать ${esc(u.name)}" title="Убрать"><svg class="ic"><use href="#i-x"/></svg></button></li>`;
  }).join("");
  formChanged();
}

async function addFiles(files) {
  for (const file of files) {
    if (uploads.length >= uploadCfg.max_files) { showFormError(`Можно приложить не больше ${uploadCfg.max_files} файлов`); break; }
    const u = { key: ++uploadSeq, name: file.name || `скриншот-${new Date().toLocaleTimeString("ru-RU").replace(/:/g, "-")}.png`, status: "uploading" };
    if (file.size > uploadCfg.max_mb * 1024 * 1024) { u.status = "error"; u.error = `больше ${uploadCfg.max_mb} МБ`; uploads.push(u); continue; }
    uploads.push(u);
    renderUploads();
    try {
      const res = await fetch(`/api/uploads?name=${encodeURIComponent(u.name)}`, { method: "POST", body: file,
        headers: { "Content-Type": file.type || "application/octet-stream" } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      u.status = "ready"; u.meta = data; u.name = data.name || u.name;
    } catch (e) {
      u.status = "error"; u.error = e.message;
    }
    renderUploads();
  }
}

$("attachBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
// файлы можно бросить на поле вопроса
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
for (const ev of ["dragenter", "dragover"]) $("dropzone").addEventListener(ev, (e) => { if (!hasFiles(e)) return; e.preventDefault(); $("dropzone").classList.add("over"); });
$("dropzone").addEventListener("dragleave", (e) => { if (!$("dropzone").contains(e.relatedTarget)) $("dropzone").classList.remove("over"); });
$("dropzone").addEventListener("drop", (e) => { $("dropzone").classList.remove("over"); if (!hasFiles(e)) return; e.preventDefault(); addFiles([...(e.dataTransfer.files || [])]); });
// вставка скриншота из буфера обмена — в любом месте формы
$("form").addEventListener("paste", (e) => {
  const stamp = () => new Date().toLocaleTimeString("ru-RU").replace(/:/g, "-");
  // браузеры называют вставленный скриншот безлико («image.png») — даём понятное имя
  const files = [...(e.clipboardData?.files || [])].map((f) => (/^image\.(png|jpe?g|gif|webp)$/i.test(f.name)
    ? new File([f], `скриншот-${stamp()}.${f.name.split(".").pop()}`, { type: f.type }) : f));
  if (files.length) { e.preventDefault(); addFiles(files); }
});
$("attList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rm]");
  if (!b) return;
  const u = uploads.find((x) => String(x.key) === b.dataset.rm);
  uploads = uploads.filter((x) => x !== u);
  if (u && u.meta && u.meta.id) fetch(`/api/uploads/${u.meta.id}`, { method: "DELETE" }).catch(() => {});
  renderUploads();
});

function showFormError(msg) {
  $("formError").hidden = !msg;
  $("formError").textContent = msg || "";
}

function renderEstimate(e) {
  const rows = e.members.map((m) =>
    `<tr><td>${esc(m.name)}</td><td>${m.calls} ${plural(m.calls, "обращение", "обращения", "обращений")}</td><td>${m.cost == null ? "?" : money(m.cost)}</td></tr>`).join("");
  let html = "";
  if (e.low != null) {
    html += `<div class="big">≈ $${e.low.toFixed(2)} – $${e.high.toFixed(2)}</div>`;
    html += `<div class="muted">${e.calls} обращений к моделям · лимит $${e.budget}</div>`;
    if (e.high > e.budget) html += `<div class="warnbox">Верхняя оценка выше лимита — заседание может остановиться, не дойдя до решения. Поднимите лимит в «Параметрах».</div>`;
    if (e.similar) html += `<div class="muted">Похожие заседания (${e.similar.count}) на деле стоили $${e.similar.min.toFixed(2)}–$${e.similar.max.toFixed(2)}, в среднем $${e.similar.avg.toFixed(2)}.</div>`;
    if (e.basis) html += `<div class="muted small">Как посчитано: ${esc(e.basis)}. Каждая модель читает все чужие варианты, поэтому цена растёт с длиной ответов и числом участников.</div>`;
    if (e.balance != null) {
      html += `<div>Баланс OpenRouter: <b>$${e.balance.toFixed(2)}</b></div>`;
      if (e.balance < e.high) html += `<div class="warnbox">На балансе меньше верхней оценки — пополните баланс, иначе заседание может прерваться.</div>`;
      else if (e.balance < e.high * 4) html += `<div class="muted small">Баланс небольшой: OpenRouter резервирует деньги под все одновременные запросы, поэтому часть запросов подождёт — заседание пройдёт, но медленнее.</div>`;
    }
  } else {
    html += `<div class="big">${e.calls} обращений к моделям</div><div class="muted">${$("mock").checked ? "Демо-режим: бесплатно, ответы ненастоящие." : "Цены моделей неизвестны."}</div>`;
    if (e.balance != null) html += `<div>Баланс OpenRouter: $${e.balance.toFixed(2)}</div>`;
  }
  if (e.warning) html += `<div class="warnbox">${esc(e.warning)}</div>`;
  const unknown = Object.entries(e.unknown || {});
  if (unknown.length) {
    html += `<div class="error">Этих моделей нет на OpenRouter — снимите участника в «Параметрах» или поправьте модель в «Настройках»:\n${
      unknown.map(([m, s]) => `• ${m}${s.length ? ` (возможно: ${s.join(", ")})` : ""}`).join("\n")}</div>`;
  }
  html += `<table>${rows}</table>`;
  $("estimate").innerHTML = html;
  const [txt, kind] = estimateSummary(e);
  $("estLine").textContent = txt;
  $("estimateBtn").className = `est-line ${kind}`;
  if (kind === "bad") { $("estimate").hidden = false; $("estimateBtn").setAttribute("aria-expanded", "true"); }
  for (const m of document.querySelectorAll(".mode-meta")) m.textContent = "";
  const meta = document.querySelector(`.mode-meta[data-meta="${mode}"]`);
  if (meta && e.low != null) meta.textContent = `≈ $${e.low.toFixed(2)}–${e.high.toFixed(2)}`;
  return unknown.length === 0;
}

async function doEstimate() {
  showFormError("");
  const p = payload();
  if (p.question.length < 3) { showFormError("Напишите вопрос для совета"); return null; }
  try {
    const e = await api("/api/estimate", { method: "POST", body: p });
    lastEstimate = e;
    return e;
  } catch (e) { showFormError(e.message); return null; }
}

let membersBound = false;
async function initForm() {
  config = await api("/api/config");
  if (config.upload) {
    uploadCfg = config.upload;
    $("fileInput").accept = uploadCfg.accept.join(",");
    const touch = matchMedia("(pointer: coarse)").matches;  // на телефоне Ctrl+V не подсказываем
    $("uploadHint").textContent = `Картинки, PDF, Word, Excel — до ${uploadCfg.max_files} файлов по ${uploadCfg.max_mb} МБ.${touch ? "" : " Скриншот можно вставить: Ctrl+V"}`;
  }
  lineup = { models: {}, extra: [], web: new Set(config.members.filter((m) => m.enabled && m.web_search).map((m) => m.name)) };
  renderMembers();
  if (!membersBound) {
    membersBound = true;
    $("members").addEventListener("change", () => { refreshRoleSelects(); renderWebMembers(); formChanged(); });
    $("webMembers").addEventListener("click", (e) => {
      const b = e.target.closest("[data-web]");
      if (!b) return;
      const n = b.dataset.web;
      if (lineup.web.has(n)) lineup.web.delete(n); else lineup.web.add(n);
      renderWebMembers(); formChanged();
    });
    $("lineupReset").addEventListener("click", () => { resetModels(); toast("Модели и роли — как в настройках"); });
    $("members").addEventListener("click", (e) => {
      const b = e.target.closest("[data-pick-model]");
      if (b) pickMemberModel(b.dataset.pickModel);
    });
    for (const id of ["chair", "devil"]) $(id).addEventListener("change", updateResetBtn);
    $("adviseBtn").addEventListener("click", () => runAdvice(false));
  }
  refreshRoleSelects();
  resetToggles();
  $("budget").value = config.budget;
  $("rounds").value = Math.max(1, config.rounds);
  $("mock").checked = !config.has_key;
  const ks = $("keyStatus");
  ks.innerHTML = config.has_key ? "OpenRouter подключён" : '<span class="hide-sm">Нет ключа — </span>только демо';
  ks.className = `pill ${config.has_key ? "ok" : "warn"}`;
  ks.title = config.has_key ? "" : "Ключ OpenRouter не задан — доступен только демо-режим";
  if (config.has_key) refreshBalance();
  setMode(config.mode === "evaluate" ? "debate" : config.mode);
}

/* остаток на балансе OpenRouter — внизу меню (на телефоне — вверху), обновляется раз в 2 минуты и после заседания */
let balanceTimer = null;
async function refreshBalance(force = false) {
  const ks = $("keyStatus");
  try {
    const r = await api(`/api/balance${force ? "?force=1" : ""}`);
    if (!r.has_key) return;
    if (r.balance == null) {
      ks.textContent = "Баланс недоступен";
      ks.className = "pill";
      ks.disabled = true;
    } else {
      const b = r.balance;
      ks.innerHTML = `<span class="hide-sm">Баланс </span>$${b.toFixed(2)}`;
      ks.className = `pill clickable ${b < 0.5 ? "bad" : b < 3 ? "warn" : "ok"}`;
      ks.disabled = false;
      const kl = r.key_limit;
      ks.title = "Остаток на балансе OpenRouter. Нажмите, чтобы пополнить."
        + (kl ? ` Лимит ключа: осталось $${kl.remaining.toFixed(2)} из $${kl.limit}.` : "");
      if (kl) {
        ks.innerHTML += ` <span class="hide-sm">· ключ $${kl.remaining.toFixed(2)}</span>`;
        if (kl.remaining < 3 && !ks.className.includes("bad")) ks.className = "pill clickable warn";
      }
      ks.setAttribute("aria-label", `Баланс OpenRouter: ${b.toFixed(2)} долларов. Открыть пополнение`);
    }
  } catch { /* оставляем прежнее значение */ }
  if (!balanceTimer) balanceTimer = setInterval(() => { if (!document.hidden) refreshBalance(); }, 120000);
}
$("keyStatus").addEventListener("click", () => {
  if ($("keyStatus").classList.contains("clickable")) window.open("https://openrouter.ai/settings/credits", "_blank", "noopener");
});

$("modeSeg").addEventListener("change", (e) => { if (e.target.name === "mode") setMode(e.target.value); });
for (const id of ["question", "options", "criteria", "rounds", "budget", "context"]) $(id).addEventListener("input", formChanged);
for (const id of ["mock", "compact", "factCheck", "chairProposes", "chair", "devil", "researchShorten", "researchWords"]) $(id).addEventListener("change", formChanged);
$("researchNoLimit").addEventListener("change", () => { updateShortenHint(); formChanged(); });
$("researchWords").addEventListener("input", () => { updateShortenHint(); updateParamsSummary(); });
for (const id of ["chairProposes", "chair"]) $(id).addEventListener("change", updateChairHint);
for (const b of document.querySelectorAll("[data-step]")) {
  b.addEventListener("click", () => {
    const r = $("rounds");
    r.value = Math.max(1, Math.min(5, (Number(r.value) || 1) + Number(b.dataset.step)));
    formChanged();
  });
}
// поле вопроса растёт вместе с текстом, чтобы не прокручивать внутри маленького окна
$("question").addEventListener("input", () => {
  const q = $("question");
  const max = window.innerHeight * 0.55;
  if (q.scrollHeight > q.clientHeight && q.clientHeight < max) { q.style.height = "auto"; q.style.height = `${Math.min(q.scrollHeight + 4, max)}px`; }
});

$("estimateBtn").addEventListener("click", async () => {
  const box = $("estimate");
  if (!box.hidden) { box.hidden = true; $("estimateBtn").setAttribute("aria-expanded", "false"); return; }
  if (!lastEstimate) {
    const e = await doEstimate();
    if (!e) return;
    renderEstimate(e);
  }
  box.hidden = false;
  $("estimateBtn").setAttribute("aria-expanded", "true");
});

$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showFormError("");
  const p = payload();
  if (p.question.length < 3) { showFormError("Напишите вопрос для совета"); $("question").focus(); return; }
  if (p.members.length < 2) { showFormError("Выберите хотя бы двух участников в «Параметрах»"); $("params").open = true; return; }
  if (mode === "evaluate" && p.options.length < 2) { showFormError("Для выбора нужно минимум 2 варианта"); $("options").focus(); return; }
  if (uploads.some((u) => u.status === "uploading")) return showFormError("Дождитесь окончания загрузки файлов");
  const btn = $("startBtn");
  btn.disabled = true;
  try {
    if (!p.mock) {
      const e = await doEstimate();
      if (!e) return;
      if (!renderEstimate(e)) { $("estimate").hidden = false; return; }
      if (e.low != null) {
        const warn = e.balance != null && e.balance < e.high ? `<p class="warnbox">На балансе $${e.balance.toFixed(2)} — может не хватить.</p>`
          : e.high > e.budget ? `<p class="warnbox">Верхняя оценка выше лимита $${e.budget} — заседание может остановиться раньше.</p>` : "";
        const go = await ask({ title: "Начать заседание?",
          html: `<p>Примерная стоимость: <b>$${e.low.toFixed(2)} – $${e.high.toFixed(2)}</b></p>
            <p class="muted">${esc(MODE_NAME[mode])} · ${p.members.length} ${plural(p.members.length, "модель", "модели", "моделей")} · лимит $${e.budget}</p>${warn}`,
          ok: "Начать" });
        if (!go) return;
      }
    }
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});
    const { id } = await api("/api/sessions", { method: "POST", body: p });
    uploads = []; renderUploads();  // файлы ушли в заседание
    setFormMode(null);
    resetLineup();
    $("question").value = ""; $("question").style.height = ""; $("context").value = ""; $("options").value = "";
    lastEstimate = null; $("estimate").hidden = true; $("estLine").textContent = "Напишите вопрос — оценим стоимость";
    openSession(id);
    loadHistory();
  } catch (e) {
    showFormError(e.message);
  } finally {
    btn.disabled = false;
  }
});

let view = "empty";
let formats = ["pdf", "docx", "md"];
const FORMAT_NAME = { pdf: "PDF", docx: "Word", md: "Markdown" };

function showView(name) {
  view = name;
  for (const v of ["empty", "session", "archive", "stats", "settings"]) $(v).hidden = name !== v;
  // в меню подсвечен текущий раздел; заседание открывается из «Нового» или архива — подсвечиваем «Архив»
  const navOf = { empty: "newBtn", archive: "archiveBtn", stats: "statsBtn", settings: "settingsBtn", session: "archiveBtn" };
  for (const b of document.querySelectorAll(".nav-item")) {
    if (b.id === navOf[name]) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  closeMenus();
  if (name !== "session") document.title = BASE_TITLE;
}

// переход по разделам — новым шагом в истории браузера, чтобы работала кнопка «Назад» (и жест «назад» на телефоне)
const sameHash = (a, b) => { try { return decodeURIComponent(a || "") === decodeURIComponent(b || ""); } catch { return a === b; } };
function go(hash) {
  const cur = location.hash === "#" || location.hash === "#/" ? "" : location.hash;
  if (sameHash(cur, hash)) return;
  history.pushState(null, "", hash || location.pathname + location.search);
}

function goCompose() {
  closeStream();
  current = null;
  showView("empty");
  markActiveHistory();
  loadPaused();
}

// напоминание на стартовом экране: есть заседания, которые можно продолжить
async function loadPaused() {
  let data;
  try { data = await api("/api/paused"); } catch { return; }
  const items = (data && data.items) || [];
  const b = $("pausedBanner");
  b.hidden = !items.length;
  if (!items.length) return;
  b.innerHTML = `<b>${items.length === 1 ? "Есть приостановленное заседание" : `Приостановленных заседаний: ${items.length}`}</b> — `
    + "уже полученные ответы сохранены, его можно продолжить без повторной оплаты:"
    + `<ul class="paused-list">${items.slice(0, 5).map((it) => `<li><a href="#/s/${encodeURIComponent(it.id)}">${esc(shortTitle(it.question, 90))}</a>`
      + ` <span class="muted small">· ${esc(it.reason_text)} · потрачено ${money(it.spent)}</span></li>`).join("")}</ul>`;
}

function goHome() {
  const wasHere = view === "empty";
  go("");
  goCompose();
  window.scrollTo({ top: 0 });
  if (wasHere || !matchMedia("(max-width: 899px)").matches) $("question").focus();
}
$("newBtn").addEventListener("click", () => { if (formMode) { setFormMode(null); resetLineup(); } goHome(); });
$("brandLink").addEventListener("click", (e) => { e.preventDefault(); goHome(); });

// part: "" — полный отчёт, "summary" — итог с обоснованием, "decision" — только решение совета
function reportUrl(id, fmt, download, part) {
  return `/api/sessions/${encodeURIComponent(id)}/report?format=${fmt}${download ? "&download=1" : ""}${part ? `&${part}=1` : ""}`;
}
function formatLinks(id, cls, part) {
  return formats.map((f) => f === "pdf"
    ? `<a class="${cls}" href="${reportUrl(id, "pdf", false, part)}" target="_blank" rel="noopener">PDF</a>`
    : `<a class="${cls}" href="${reportUrl(id, f, true, part)}">${FORMAT_NAME[f]}</a>`).join("");
}
const REPORT_PARTS = [
  ["decision", "Только решение", "Решение совета без вопроса — чтобы переслать"],
  ["summary", "Итог с обоснованием", "Решение, почему, риски и особые мнения"],
  ["", "Полный отчёт", "Всё заседание: варианты, критика, голосование, расходы"],
];
// меню скачивания: три вида отчёта, у каждого — форматы и «открыть в браузере»
function downloadLinks(id, cls = "btn small") {
  return REPORT_PARTS.map(([part, label, hint]) =>
    `<div class="dl-group"><span class="dl-label">${label}</span><span class="dl-hint">${hint}</span><div class="dl-row">${formatLinks(id, cls, part)}`
    + `<a class="${cls}" href="${reportUrl(id, "html", false, part)}" target="_blank" rel="noopener" aria-label="${label} — открыть в новой вкладке">Открыть<svg class="ic" aria-hidden="true" style="width:15px;height:15px"><use href="#i-ext"/></svg></a></div></div>`).join("");
}

/* выпадающие меню (скачать): одно открыто за раз, закрываются по клику мимо и Esc */
function toggleMenu(btn, menu) {
  const open = menu.hidden;
  closeMenus();
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) { const first = menu.querySelector("a, button"); if (first) first.focus(); }
}
function closeMenus() {
  for (const m of document.querySelectorAll(".menu")) m.hidden = true;
  for (const b of document.querySelectorAll("[aria-haspopup]")) b.setAttribute("aria-expanded", "false");
}
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeMenus(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const open = document.querySelector(".menu:not([hidden])");
  if (open) { const b = open.parentElement.querySelector("[aria-haspopup]"); closeMenus(); if (b) b.focus(); }
});
$("dlBtn").addEventListener("click", () => toggleMenu($("dlBtn"), $("downloads")));

/* ------------------------------------------------------------ недавние заседания (в меню слева) */

const HISTORY_LIMIT = 12;
const STATUS_TEXT = { running: "идёт", error: "прервано", cancelled: "остановлено",
  paused: '<span class="hist-paused">на паузе</span>', interrupted: '<span class="hist-paused">прервано — можно продолжить</span>' };
function niceDate(it) {
  const d = it.date || it.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/, "$3.$2.$1 $4:$5");
  const m = String(d).match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}:\d{2})/);
  if (!m) return d;
  const day = new Date(+m[3], +m[2] - 1, +m[1]);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return `сегодня, ${m[4]}`;
  if (diff === 1) return `вчера, ${m[4]}`;
  return day.toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: day.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

async function loadHistory() {
  let data;
  try { data = await api("/api/sessions"); } catch { return; }
  formats = data.formats || formats;
  const items = data.items || [];
  const ul = $("history");
  $("allArchiveBtn").hidden = items.length <= HISTORY_LIMIT;
  $("allArchiveBtn").textContent = `Весь архив (${data.total}) →`;
  if (!items.length) { ul.innerHTML = '<li class="muted small" style="padding:0 .75rem">Пока пусто</li>'; return; }
  ul.innerHTML = items.slice(0, HISTORY_LIMIT).map((it) => {
    const meta = it.status === "done"
      ? `${esc(niceDate(it))} · ${esc(MODE_NAME[it.mode] || it.mode_name || "")}${it.unacceptable ? " · ни один не подходит" : ""}`
      : `${it.status === "running" ? '<span class="dot-run"></span>' : ""}${esc(niceDate(it))} · ${STATUS_TEXT[it.status] || esc(it.status)}${it.spent ? ` · ${money(it.spent)}` : ""}`;
    return `<li class="item" data-id="${esc(it.id)}"><button type="button" class="open" data-open-id="${esc(it.id)}" title="${esc(shortTitle(it.question, 300))}">
      <span class="q">${esc(shortTitle(it.question))}</span><span class="meta">${meta}</span></button>
      ${it.status === "running" ? "" : `<button type="button" class="icon-btn del" data-del="${esc(it.id)}" aria-label="Удалить заседание «${esc(shortTitle(it.question, 60))}»" title="Удалить"><svg class="ic" style="width:16px;height:16px"><use href="#i-trash"/></svg></button>`}</li>`;
  }).join("");
  markActiveHistory();
}

async function deleteSession(id, title) {
  const ok = await ask({ title: "Удалить заседание?", html: `<p>«${esc(title)}» и все его отчёты будут удалены. Это нельзя отменить.</p>`, ok: "Удалить", danger: true });
  if (!ok) return false;
  try { await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (err) { toast(err.message); return false; }
  toast("Заседание удалено");
  return true;
}

/* ------------------------------------------------------------ архив */

let archiveTimer = null;
async function loadArchive() {
  const q = $("archiveSearch").value.trim();
  const mode = $("archiveMode").value;
  let data;
  try { data = await api(`/api/sessions?q=${encodeURIComponent(q)}&mode=${encodeURIComponent(mode)}`); }
  catch (e) { $("archiveList").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  formats = data.formats || formats;
  const items = data.items || [];
  $("archiveCount").textContent = q || mode ? `Найдено: ${items.length}` : `${items.length} ${plural(items.length, "заседание", "заседания", "заседаний")}`;
  if (!items.length) {
    $("archiveList").innerHTML = `<div class="arc-empty">${q || mode ? "Ничего не найдено. Попробуйте другое слово." : "Здесь появятся ваши заседания. Задайте первый вопрос в разделе «Новый»."}</div>`;
    return;
  }
  $("archiveList").innerHTML = items.map((it) => {
    const done = it.status === "done";
    const meta = [niceDate(it), MODE_NAME[it.mode] || it.mode_name, it.members && it.members.length ? `${it.members.length} ${plural(it.members.length, "модель", "модели", "моделей")}` : "",
      it.attachments ? `${it.attachments} ${plural(it.attachments, "файл", "файла", "файлов")}` : "",
      it.followup_of ? "уточнение" : "", it.cost != null ? money(it.cost) : ""]
      .filter(Boolean).map(esc).join(" · ");
    const status = done ? "" : `<span class="arc-status ${esc(it.status)}">${STATUS_TEXT[it.status] || esc(it.status)}</span> `;
    const del = it.status === "running" ? "" : `<button type="button" class="icon-btn" data-arc-del="${esc(it.id)}" aria-label="Удалить" title="Удалить"><svg class="ic"><use href="#i-trash"/></svg></button>`;
    const dl = done ? `<div class="menu-wrap"><button type="button" class="btn small" aria-haspopup="true" aria-expanded="false" data-menu="m-${esc(it.key || it.id)}"><svg class="ic" aria-hidden="true"><use href="#i-dl"/></svg>Скачать</button>
      <div class="menu" id="m-${esc(it.key || it.id)}" hidden>${downloadLinks(it.id)}</div></div>` : "";
    return `<article class="arc-item">
      <div class="arc-q" data-open="${esc(it.id)}" tabindex="0" role="link" title="${esc(shortTitle(it.question, 300))}">${status}${esc(shortTitle(it.question))}</div>
      <div class="arc-actions">${dl}${del}</div>
      ${done && it.winner && it.unacceptable ? `<div class="arc-win">⛔ Ни один вариант не подходит <span class="muted">· лидер сравнения — ${esc(it.winner)}</span></div>` : ""}
      ${done && it.winner && !it.unacceptable ? `<div class="arc-win"><span class="lbl" style="width:24px;height:24px;font-size:.78rem;margin-right:.35rem;vertical-align:-2px">${esc(it.winner)}</span>${esc(it.winner_title)} <span class="muted">· ${esc(it.winner_author)}</span></div>` : ""}
      <div class="arc-meta">${meta}</div>
    </article>`;
  }).join("");
}

function openArchive() {
  closeStream();
  current = null;
  go("#/archive");
  showView("archive");
  markActiveHistory();
  loadArchive();
}

$("archiveBtn").addEventListener("click", openArchive);
$("allArchiveBtn").addEventListener("click", openArchive);
$("archiveSearch").addEventListener("input", () => { clearTimeout(archiveTimer); archiveTimer = setTimeout(loadArchive, 250); });
$("archiveMode").addEventListener("change", loadArchive);
$("archiveList").addEventListener("click", async (e) => {
  const menuBtn = e.target.closest("[data-menu]");
  if (menuBtn) { toggleMenu(menuBtn, $(menuBtn.dataset.menu)); return; }
  const del = e.target.closest("[data-arc-del]");
  if (del) {
    const title = del.closest(".arc-item").querySelector(".arc-q").textContent;
    if (await deleteSession(del.dataset.arcDel, title)) { loadArchive(); loadHistory(); }
    return;
  }
  const open = e.target.closest("[data-open]");
  if (open) openSession(open.dataset.open);
});
$("archiveList").addEventListener("keydown", (e) => {
  const open = e.target.closest("[data-open]");
  if (open && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSession(open.dataset.open); }
});

$("history").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    const title = del.closest("li").querySelector(".q").textContent;
    if (await deleteSession(del.dataset.del, title)) {
      if (current && current.id === del.dataset.del) { go(""); goCompose(); }
      loadHistory();
    }
    return;
  }
  const b = e.target.closest("[data-open-id]");
  if (b) openSession(b.dataset.openId);
});

function markActiveHistory() {
  for (const li of $("history").querySelectorAll("li.item")) {
    const on = !!current && li.dataset.id === current.id;
    li.classList.toggle("active", on);
    const b = li.querySelector(".open");
    if (b) { if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); }
  }
}

/* ------------------------------------------------------------ заседание: состояние */

let current = null;
let stream = null;
let timer = null;

function newState(id) {
  return {
    id, question: "", mode: "", rounds: null, members: [], chair: null, mock: false,
    refined: "", criteria: [], stage: null, stageTitle: {}, doneStages: new Set(),
    work: {}, proposals: {}, authors: {}, revised: {}, critiques: {}, ballots: {},
    tally: null, verdict: null, failures: [], cost: 0, done: null, error: null,
    startedAt: Date.now(), openedAt: Date.now(), at: Date.now(), initial: {},
    calls: {}, log: [], stageStart: null, lastMsg: Date.now(), wrote: {},
    attachments: [], blind: [], imageNotes: null, imageNotesBy: "",
  };
}

const PHASE_ACTIVE = new Set(["queued", "sent", "thinking", "writing", "checking", "retry"]);
function logAdd(s, text, kind = "") {
  s.log.push({ t: new Date(s.at || Date.now()), text, kind });
  if (s.log.length > 400) s.log.shift();
}
// короткий заголовок: первая содержательная строка без разметки, до 110 символов (как в отчётах)
function shortTitle(text, limit = 110) {
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.replace(/^\s*(#+|[-*+•>]|\d+[.)])\s+/, "").replace(/[*_`]+/g, "").trim();
    if (!line) continue;
    const manyLines = String(text).split(/\r?\n/).filter((x) => x.trim()).length > 1;
    // длинный запрос: в заголовок — первое предложение, если оно короткое
    const m = line.match(/^(.{12,90}?[.!?…])(\s|$)/);
    if (m && (manyLines || line.length > limit)) return m[1];
    if (line.length <= limit) return line;
    const part = line.slice(0, limit);
    const cut = part.includes(" ") ? part.slice(0, part.lastIndexOf(" ")) : part;
    return cut.replace(/[,;:—–\- ]+$/, "") + "…";
  }
  return "";
}
const fmtSec = (sec) => (sec >= 60 ? `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}` : `${Math.floor(sec)} с`);
const approxTokens = (chars) => Math.max(1, Math.round(chars / 3.2));

function apply(s, ev) {
  // когда событие случилось на сервере; часы браузера могут спешить — не позже «сейчас»
  s.at = ev.ts ? Math.min(ev.ts * 1000, Date.now()) : Date.now();
  switch (ev.type) {
    case "ping":
      break;
    case "followup":
      logAdd(s, `Уточнение: совет видит ${ev.thread.length} предыдущ${ev.thread.length > 1 ? "их решения" : "ее решение"}`);
      break;
    case "attachments":
      s.attachments = ev.items || [];
      s.blind = ev.blind || [];
      logAdd(s, `Вложения: ${s.attachments.map((a) => a.name).join(", ")}`
        + (s.blind.length ? ` · без зрения: ${s.blind.join(", ")} — им опишут картинки` : ""));
      break;
    case "image_notes":
      s.imageNotes = ev.notes || {}; s.imageNotesBy = ev.by;
      logAdd(s, `${ev.by} описал(а) картинки для моделей без зрения`, "k-ok");
      break;
    case "saving":
      s.saving = true;
      logAdd(s, "Готовлю отчёты: PDF, Word, Markdown");
      break;
    case "call": {
      const prev = s.calls[ev.member];
      const fresh = !prev || prev.stage !== ev.stage;
      const c = fresh ? { stage: ev.stage, chars: 0, rchars: 0 } : prev;
      c.phase = ev.phase;
      c.since = s.at - (ev.elapsed || 0) * 1000;  // начало обращения — по времени сервера
      c.updated = s.at;
      if (ev.chars != null) c.chars = ev.chars;
      if (ev.reasoning_chars != null) c.rchars = ev.reasoning_chars;
      if (ev.attempt) c.attempt = ev.attempt;
      if (ev.max_tokens) c.maxTokens = ev.max_tokens;
      c.detail = ev.detail || (ev.phase === "retry" ? c.detail : "");
      if (ev.wait) c.wait = ev.wait;
      if (ev.cost != null) c.cost = ev.cost;
      if (["done", "failed"].includes(ev.phase)) c.final = ev.elapsed;
      s.calls[ev.member] = c;
      const who = `${ev.member} (${STAGE_NAME[ev.stage] || ev.stage})`;
      const key = `${ev.member}|${ev.stage}`;
      if (ev.phase === "sent" && (ev.attempt || 1) === 1 && fresh) logAdd(s, `${who}: запрос отправлен`);
      if (ev.phase === "writing" && !s.wrote[key]) { s.wrote[key] = 1; logAdd(s, `${who}: начал писать ответ через ${fmtSec(ev.elapsed || 0)}`); }
      if (ev.phase === "retry") logAdd(s, `${who}: повтор — ${ev.detail || "временная ошибка"}${ev.wait ? `, через ${ev.wait} с` : ""}`, "k-warn");
      if (ev.phase === "done") logAdd(s, `${who}: готово за ${fmtSec(ev.elapsed || 0)}`, "k-ok");
      if (ev.phase === "failed") logAdd(s, `${who}: сбой — ${ev.detail || ""}`, "k-bad");
      break;
    }
    case "start":
      if (!s.startSeen) { s.startSeen = true; s.startedAt = s.at; }  // после продолжения — время от первого старта
      Object.assign(s, { question: ev.question, mode: ev.mode, rounds: ev.rounds, members: ev.members || [],
        chair: ev.chair || null, mock: !!ev.mock, parent: ev.parent || null, factPlanned: !!ev.fact_check,
        budget: ev.budget || null });
      break;
    case "resumed":
      s.cost = ev.spent || s.cost;
      logAdd(s, `▶ Продолжение: ${ev.replayable} ${plural(ev.replayable, "готовый ответ берётся", "готовых ответа берутся", "готовых ответов берутся")} из сохранённого — без повторной оплаты`, "k-ok");
      break;
    case "budget_warning":
      s.budgetWarn = ev;
      logAdd(s, `Потрачено ${money(ev.spent)} из бюджета ${money(ev.budget)} — бюджета может не хватить`, "k-warn");
      break;
    case "budget":
      s.budget = ev.budget; s.budgetWarn = null;
      logAdd(s, `Бюджет заседания поднят до ${money(ev.budget)}`, "k-ok");
      break;
    case "paused":
      s.paused = ev; s.stage = null; s.work = {};
      if (ev.budget) s.budget = ev.budget;
      if (ev.spent) s.cost = ev.spent;
      logAdd(s, `Заседание приостановлено: ${ev.reason_text}${ev.message && ev.reason !== "stopped" ? ` (${ev.message})` : ""}`, "k-warn");
      break;
    case "stage": {
      s.stageStart = s.at;
      logAdd(s, `▶ ${ev.title}`, "k-stage");
      if (s.stage && s.stage !== ev.stage) s.doneStages.add(s.stage);
      s.stage = ev.stage;
      s.stageTitle[ev.stage] = ev.title;
      if (ev.stage === "proposal") s.chairJudge = ev.chair_proposes === false;
      // председатель без своего варианта не участвует в предложениях и доработке
      const who = ev.stage === "factcheck" ? ["факт-чек"]
        : ["framing", "verdict"].includes(ev.stage) ? [s.chair].filter(Boolean)
        : ["proposal", "revision"].includes(ev.stage) && s.chairJudge ? s.members.filter((m) => m !== s.chair) : s.members;
      s.work = Object.fromEntries(who.map((n) => [n, "work"]));
      break;
    }
    case "lenses":
      s.lensOf = ev.by || {};
      if (s.lenses && s.lenses.length) renderFraming(s);
      break;
    case "framing":
      s.refined = ev.refined_question; s.criteria = ev.criteria; s.requirements = ev.requirements || [];
      s.scope = [ev.scope, ev.as_of ? `актуально на ${ev.as_of}` : ""].filter(Boolean).join(" · ");
      s.subq = ev.subquestions || []; s.lenses = ev.lenses || [];
      if (ev.lenses) s.lensOf = {};
      logAdd(s, `Критерии: ${(ev.criteria || []).join(", ")}`);
      if (s.stage === "framing") s.work = {};
      s.doneStages.add("framing");
      break;
    case "member_done":
      s.work[ev.member] = "done";
      if (ev.stage === "critique") s.critiques[ev.member] = ev.result;
      if (ev.stage === "vote") s.ballots[ev.member] = ev.result;
      break;
    case "member_failed":
      s.work[ev.member] = "fail";
      s.failures.push(ev);
      break;
    case "proposals":
      s.proposals = { ...ev.proposals }; s.initial = { ...ev.proposals }; s.authors = ev.authors;
      break;
    case "revisions":
      for (const [l, p] of Object.entries(ev.revised)) { s.proposals[l] = p; s.revised[l] = true; }
      break;
    case "tally":
      s.tally = ev.rows;
      s.agreement = ev.agreement || null;
      s.ballotFlags = ev.ballot_flags || {};
      s.coLeaders = ev.co_leaders || [];
      s.decisive = ev.decisive || {};
      s.coverage = ev.coverage || null;
      s.sections = ev.sections || {};
      s.leaderWithout = ev.leader_without_flagged || null;
      if (ev.rows && ev.rows[0]) logAdd(s, `Голосование: лидирует вариант ${ev.rows[0].label}`);
      break;
    case "warning":
      logAdd(s, `⚠️ ${ev.member}${ev.stage ? ` (${STAGE_NAME[ev.stage] || ev.stage})` : ""}: ${ev.text}`, "k-warn");
      break;
    case "ledger":
      s.ledger = ev.ledger || null;
      if (s.stage === "arbitration") s.work = {};
      logAdd(s, `Проверка по источникам: проверено ${(s.ledger && s.ledger.claims || []).length} утверждений, источников ${(s.ledger && s.ledger.evidence || []).length}`, "k-ok");
      break;
    case "factcheck":
      s.facts = ev.results || {};
      s.factModel = ev.model || "";
      if (s.stage === "factcheck") s.work = {};
      logAdd(s, `Проверка фактов: ${Object.keys(s.facts).join(", ") || "нет результатов"}`);
      break;
    case "cost":
      s.cost = ev.total;
      break;
    case "verdict":
      s.verdict = ev.verdict;
      s.ideas = ev.ideas || [];
      logAdd(s, ev.verdict.acceptable === false ? `Председатель: ни один вариант не подходит (лидер сравнения — ${ev.verdict.winner})`
        : `Председатель выбрал вариант ${ev.verdict.winner}`, ev.verdict.acceptable === false ? "k-warn" : "k-ok");
      break;
    case "done":
      setTimeout(() => refreshBalance(true), 3000);  // расход заседания уже списан
      logAdd(s, `✓ Заседание завершено, отчёты готовы`, "k-ok");
      s.done = ev;
      s.semantic = ev.semantic || null;
      s.cost = ev.total_cost ?? s.cost;
      if (s.stage) s.doneStages.add(s.stage);
      s.stage = null; s.work = {};
      break;
    case "error":
      logAdd(s, `Заседание прервано: ${ev.message}`, "k-bad");
      s.error = ev.message; s.stage = null; s.work = {};
      break;
  }
}

/* ------------------------------------------------------------ заседание: отрисовка */

function authorOf(s, label) {
  const a = s.authors[label];
  if (a == null) return "пользователь";
  if ($("blind").checked && !s.verdict) return "автор скрыт";
  return a;
}

function scoreColor(v) {
  const t = Math.max(0, Math.min(1, (v - 1) / 9));
  const hue = Math.round(t * 130);
  return `background: hsl(${hue} 65% 50% / .2)`;
}

// этапы, которые реально будут в этом заседании
function stagePlan(s) {
  const skip = new Set(s.mode === "quick" ? ["critique", "revision"] : s.mode === "evaluate" ? ["proposal", "revision"] : []);
  const plan = STAGES.filter(([k]) => !skip.has(k));
  if (s.mode === "research") {  // свой арбитраж доказательств вместо отдельной проверки фактов
    plan.splice(plan.findIndex(([k]) => k === "revision"), 0, ARB_STAGE);
    if (s.stageTitle.recheck || s.doneStages.has("recheck"))  // проверка новых утверждений после доработки
      plan.splice(plan.findIndex(([k]) => k === "revision") + 1, 0, ["recheck", "Проверка новых утверждений"]);
    return plan.map(([k, t]) => [k, RESEARCH_STAGE[k] ? RESEARCH_STAGE[k][0] : t]);
  }
  if (s.stageTitle.factcheck || s.facts || s.factPlanned) plan.splice(plan.length - 1, 0, ["factcheck", STAGE_NAME.factcheck]);
  return plan;
}

function render(s) {
  if (view !== "session") return;
  updateTitle();
  const finished = !!(s.done || s.error || s.paused);

  $("sMeta").textContent = [MODE_NAME[s.mode] || s.mode, s.mode === "debate" && s.rounds > 1 ? `${s.rounds} раунда` : "",
    s.members.length ? `${s.members.length} ${plural(s.members.length, "модель", "модели", "моделей")}` : "", s.mock ? "демо-режим" : ""].filter(Boolean).join(" · ");
  $("sParent").hidden = !s.parent;
  if (s.parent) $("sParent").innerHTML = `↳ Уточнение к заседанию <a href="#/s/${encodeURIComponent(s.parent.id)}">${esc(s.parent.title || "…")}</a>`;
  $("nextActions").hidden = !s.done;
  if (!s.done) { $("rateRow").hidden = true; $("costSec").hidden = true; $("pilotSec").hidden = true; }
  else if (!s.rateLoaded) { s.rateLoaded = true; loadRating(s.id); loadCosts(s.id); loadPilot(s); }
  const title = shortTitle(s.question);
  $("sQuestion").textContent = title;
  const longQuery = !!s.question && s.question.trim() !== title;
  $("sQuery").hidden = !longQuery;
  if (longQuery && $("sQueryBody").dataset.src !== s.question) {
    $("sQueryBody").dataset.src = s.question;
    $("sQueryBody").innerHTML = md(s.question);  // переносы строк, списки и выделение — как написал пользователь
    $("sQueryBody").classList.remove("open");
    requestAnimationFrame(() => {
      const b = $("sQueryBody");
      const clipped = b.scrollHeight > b.clientHeight + 2;
      b.classList.toggle("clipped", clipped);
      b.dataset.clipped = clipped ? "1" : "";
      queryToggle(finished);
    });
  }
  // после завершения исходный вопрос свёрнут: первым на экране — вывод
  if (longQuery && $("sQuery").classList.contains("folded") !== finished && !$("sQueryBody").classList.contains("open")) {
    $("sQuery").classList.toggle("folded", finished);
    queryToggle(finished);
  }
  // на основном экране — округлённо; точная сумма — в подсказке и в «Куда ушли деньги»
  $("sCost").textContent = s.cost == null ? "—" : s.cost > 0 && s.cost < 0.01 ? "<$0.01" : `$${Number(s.cost).toFixed(2)}`;
  $("sCost").title = money(s.cost);
  $("session").classList.toggle("finished", finished);
  const running = !finished;
  $("sBudget").textContent = s.budget && !s.done ? ` из $${Number(s.budget) % 1 ? Number(s.budget).toFixed(2) : Number(s.budget)}` : "";
  $("budgetBtn").hidden = !running || !s.budget;
  $("sBudgetWarn").hidden = !(running && s.budgetWarn);
  if (running && s.budgetWarn) {
    $("sBudgetWarn").innerHTML = `<b>Бюджет почти исчерпан:</b> потрачено ${money(s.cost)} из ${money(s.budget)}. `
      + `Если его не хватит, заседание приостановится, а не пропадёт — но можно поднять бюджет сейчас, и оно не прервётся. `
      + `<button type="button" class="link" data-raise="1">Поднять бюджет</button>`;
  }
  renderPaused(s);
  $("sError").hidden = !s.error;
  $("sError").innerHTML = s.error ? `<b>Заседание прервано.</b> ${esc(s.error)}<br><a href="/api/sessions/${encodeURIComponent(s.id)}/diagnostics">Скачать журнал для разбора</a>` : "";
  $("diagLink").href = `/api/sessions/${encodeURIComponent(s.id)}/diagnostics`;

  renderAttachments(s);
  renderFraming(s);
  renderActivity(s);

  // расходы по моделям — в журнале
  if (s.done && s.done.stats) {
    $("memberStatus").innerHTML = Object.entries(s.done.stats).map(([n, st]) =>
      `<span class="chip ${st.failures && st.failures.length ? "fail" : "done"}" title="${st.calls} обращений">${esc(n)} · ${money(st.cost)}</span>`).join("");
  } else $("memberStatus").innerHTML = "";

  renderVerdict(s);
  renderTally(s);
  renderProposals(s);
  renderMatrix(s);

  $("failSec").hidden = !s.failures.length;
  $("redoBox").hidden = !(s.done && s.failures.length);
  $("failures").innerHTML = s.failures.map((f) =>
    `<li><b>${esc(f.member)}</b> (${esc(STAGE_NAME[f.stage] || f.stage)}): ${esc(f.error)}</li>`).join("");
}

// «Как совет понял задачу»: уточнённый вопрос, критерии и обязательные требования (после этапа «Постановка»)
// приостановлено: причина, сколько потрачено, продолжить (с новым бюджетом) или удалить
const PAUSE_HELP = {
  budget: "Поднимите бюджет и нажмите «Продолжить».",
  key_limit: 'Поднимите или снимите лимит ключа в <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">настройках ключей OpenRouter</a> (или дождитесь сброса лимита) и нажмите «Продолжить».',
  credits: 'Пополните баланс на <a href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener">OpenRouter</a> и нажмите «Продолжить».',
  interrupted: "Нажмите «Продолжить» — заседание пойдёт с того места, где остановилось.",
  stopped: "Его можно продолжить с того места, где оно остановилось.",
  error: "Можно попробовать продолжить: модели, которые не ответили на последнем этапе, спросим заново.",
};
function renderPaused(s) {
  const p = s.paused;
  $("sPaused").hidden = !p;
  if (!p) return;
  $("pReason").innerHTML = `<b>Причина:</b> ${esc(p.reason_text || "")}${p.message && ["error", "credits", "key_limit"].includes(p.reason) ? ` — <span class="muted">${esc(p.message)}</span>` : ""}. ${PAUSE_HELP[p.reason] || ""}`;
  $("pSpent").textContent = `Потрачено ${money(p.spent || s.cost)} из бюджета ${money(p.budget || s.budget)}`
    + `${p.stage ? ` · остановилось на этапе «${p.stage}»` : ""}. Уже полученные ответы моделей сохранены: при продолжении они не оплачиваются повторно.`;
  const inp = $("pBudget");
  if (inp.dataset.for !== s.id + p.reason) {
    inp.dataset.for = s.id + p.reason;
    const b = Number(p.budget || s.budget || 1), spent = Number(p.spent || 0);
    inp.value = p.reason === "budget" || b <= spent ? Math.ceil(Math.max(b * 1.5, spent + 1) * 2) / 2 : b;
  }
}

async function resumeSession(id) {
  const budget = Number($("pBudget").value) || null;
  $("pResume").disabled = true;
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST", body: { budget } });
    openSession(id);
    loadHistory();
  } catch (e) { toast(e.message); } finally { $("pResume").disabled = false; }
}

async function raiseBudget() {
  const s = current;
  if (!s) return;
  const suggest = Math.ceil(Math.max(Number(s.budget || 1) * 1.5, Number(s.cost || 0) + 1) * 2) / 2;
  const ok = await ask({ title: "Поднять бюджет заседания", ok: "Поднять",
    html: `<p>Потрачено ${money(s.cost)} из ${money(s.budget)}. Новый лимит действует сразу — заседание продолжится без остановки.</p>`
      + `<label class="field"><span>Новый бюджет, $</span><input id="newBudget" type="number" min="0.1" step="0.5" value="${suggest}" inputmode="decimal"></label>` });
  if (!ok) return;
  const val = Number((document.getElementById("newBudget") || {}).value) || suggest;
  try {
    await api(`/api/sessions/${encodeURIComponent(s.id)}/budget`, { method: "POST", body: { budget: val } });
    s.budget = val; s.budgetWarn = null; scheduleRender();
  } catch (e) { toast(e.message); }
}

// итог исследования: известно / спорно / неизвестно, подвопросы с каркасом, источники
const CLAIM_BADGE = { confirmed: ["ok", "подтверждено"], refuted: ["bad", "опровергнуто"], contested_in_sources: ["warn", "спорно в источниках"],
  unclear: ["plain", "не удалось проверить"], user_doc: ["plain", "из документов"], search_unavailable: ["plain", "поиск недоступен"], not_checked: ["plain", "не проверялось"],
  not_public: ["plain", "поиском не проверить"] };
const COVER_WORD = { covered: "раскрыт", partial: "частично", unknown: "без ответа" };
const GAP_WORD = { unverified_memory: "только по памяти моделей", no_public_data: "нет открытых данных", not_checked: "не проверено" };
function renderResearch(s) {
  const v = s.verdict || {};
  const box = $("vResearch");
  const isR = s.mode === "research" && (v.known || v.contested || v.unknown);
  box.hidden = !isR;
  $("vCard").innerHTML = "";
  if (!isR) { box.innerHTML = ""; return; }
  const ev = Object.fromEntries(((s.ledger && s.ledger.evidence) || []).map((e) => [e.id, e]));
  const refs = (ids) => (ids || []).map((i) => i === "DOC" ? '<span class="lbl">DOC</span>'
    : ev[i] ? `<a href="${esc(ev[i].url)}" target="_blank" rel="noopener" title="${esc(ev[i].title || ev[i].url)}">[${esc(i)}]</a>` : "").join(" ");
  const card = v.decision_card || {};
  const cardList = (t, xs) => ((xs || []).length ? `<div><h4>${t}</h4><ul>${xs.map((x) => `<li>${inline(x)}</li>`).join("")}</ul></div>` : "");
  // карточка решения — первой, до текста итога: решение, известное, пробелы, шаги
  $("vCard").innerHTML = card.summary_decision || (card.top_known || []).length
    ? `<div class="decision-card"><div class="dc-title">Карточка решения</div>${card.summary_decision ? `<p class="dc-sum">${inline(card.summary_decision)}</p>` : ""}
      <div class="dc-grid">${cardList("Известно", card.top_known)}${cardList("Главные пробелы", card.key_unknowns)}${cardList("Следующие шаги", card.next_steps)}</div>
      ${(card.unverified || []).length ? `<div class="dc-unverified">⚠ <b>В карточке не подтверждено:</b> ${card.unverified.map(inline).join("; ")}</div>` : ""}</div>` : "";
  let h = `<h3>Что известно, что спорно, чего не знает никто</h3><p class="muted small">«Известно» — только то, что подтвердили найденные источники или ваши документы; согласие моделей сюда не попадает.</p>`;
  h += `<div class="kcu"><div class="kcu-col ok"><h4>Известно</h4>${(v.known || []).length ? `<ul>${v.known.map((k) => `<li>${inline(k.statement)} ${refs(k.evidence_ids)}${k.volatility === "changes_fast" ? ' <span class="muted small">· быстро меняется</span>' : ""}${knownBasis(k, s)}</li>`).join("")}</ul>` : '<p class="muted small">Подтверждённых источниками утверждений нет.</p>'}</div>`;
  h += `<div class="kcu-col warn"><h4>Спорно</h4>${(v.contested || []).length ? `<ul>${v.contested.map((c) => `<li><b>${inline(c.issue)}</b>${(c.positions || []).length ? `<div class="small">${c.positions.map(inline).join(" · ")}</div>` : ""}${c.why_they_differ ? `<div class="muted small">почему: ${inline(c.why_they_differ)}</div>` : ""} ${refs(c.evidence_ids)}</li>`).join("")}</ul>` : '<p class="muted small">Нет.</p>'}</div>`;
  h += `<div class="kcu-col bad"><h4>Неизвестно</h4>${(v.unknown || []).length ? `<ul>${v.unknown.map((u) => `<li>${inline(u.gap)}${u.reason ? ` <span class="muted small">· ${esc(GAP_WORD[u.reason] || u.reason)}</span>` : ""}${u.how_to_check ? `<div class="muted small">как узнать: ${inline(u.how_to_check)}</div>` : ""}</li>`).join("")}</ul>` : '<p class="muted small">Нет.</p>'}</div></div>`;
  const subq = s.subq || [];
  if (subq.length) {
    const audit = Object.fromEntries((v.coverage_audit || []).map((c) => [c.subquestion_id, c]));
    const bb = v.section_backbone || {};
    h += `<h4>Подвопросы</h4><ul class="subq">${subq.map((q, i) => {
      const id = `Q${i + 1}`, a = audit[id] || {}, sec = (s.sections || {})[id] || {};
      const lead = bb[id] || sec.leader;
      return `<li><span class="rq">${id}</span> ${esc(q)} <span class="muted small">· ${esc(COVER_WORD[a.status] || a.status || "—")}${lead ? ` · каркас — вариант ${esc(lead)}` : ""}</span></li>`;
    }).join("")}</ul>`;
  }
  h += entityCoverage(s);
  if ((v.next_questions || []).length) h += `<h4>Что исследовать дальше</h4><ul>${v.next_questions.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`;
  const claims = [...((s.ledger && s.ledger.claims) || []).map((c) => (c.duplicate_of
    ? { ...c, notes: [`то же утверждение, что ${c.duplicate_of} — проверено вместе`, ...(c.notes || [])] } : c)),
    ...((s.ledger && s.ledger.deferred) || []).map((d) => ({ id: (d.ids || []).join(", "), text: d.text, status: "not_public", notes: [d.how_to_check], evidence: [] }))];
  if (claims.length) {
    h += `<details class="sources"><summary>Проверка утверждений и источники (${claims.length} утв., ${Object.keys(ev).length} источн.)</summary><ul class="claims">${claims.map((c) => {
      const [k, t] = CLAIM_BADGE[c.status] || CLAIM_BADGE.not_checked;
      return `<li><span class="badge ${k}">${t}</span> <span class="lbl">${esc(c.id)}</span> ${inline(c.text)} ${refs(c.evidence)}${c.quote_found ? ' <span class="badge ok" title="Дословная фраза, которую привёл поиск, найдена на странице источника">цитата найдена на странице</span>' : ""}${(c.notes || []).length ? `<div class="muted small">${c.notes.map(inline).join(" · ")}</div>` : ""}</li>`;
    }).join("")}</ul><ol class="srcs">${Object.values(ev).map((e) => `<li><b>${esc(e.id)}</b> <a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title || e.url)}</a> <span class="muted small">${esc([e.publisher, e.date].filter(Boolean).join(", "))}${e.search_returned === false ? " · нет в результатах поиска" : ""}</span>${LINK_BADGE[e.link] || ""}</li>`).join("")}</ol></details>`;
  }
  box.innerHTML = h;
}

function renderFraming(s) {
  const reqs = s.requirements || [];
  const refined = s.refined && s.refined.trim() !== (s.question || "").trim() ? s.refined : "";
  const any = !!(s.criteria.length || reqs.length || refined || (s.subq || []).length);
  $("frameSec").hidden = !any;
  if (!any) return;
  $("fRefined").hidden = !refined;
  $("fRefined").innerHTML = refined ? `<span class="muted">Уточнённый вопрос:</span> ${esc(refined)}` : "";
  $("fCritRow").hidden = !s.criteria.length;
  $("fCriteria").innerHTML = s.criteria.map((c) => `<li>${esc(c)}</li>`).join("");
  $("fScopeRow").hidden = !s.scope;
  $("fScope").textContent = s.scope || "";
  const subq = s.subq || [];
  $("fSubqRow").hidden = !subq.length;
  $("fSubq").innerHTML = subq.map((q, i) => `<li><span class="rq">Q${i + 1}</span><span>${esc(q)}</span></li>`).join("");
  const lenses = s.lenses || [];
  $("fLensRow").hidden = !lenses.length;
  const who = (l) => Object.entries(s.lensOf || {}).filter(([, t]) => t === l).map(([m]) => m);
  $("fLenses").innerHTML = lenses.map((l) => {
    const m = l.match(/^(.{2,60}?)\s*(?::|—| - )\s*(.+)$/s);  // «рынок и данные: цифры, цены…» → заголовок и пояснение
    const authors = who(l);
    return `<li>${authors.length ? `<div class="lens-who">${authors.map(esc).join(", ")}</div>` : ""}<div class="lens-head"><b>${esc(m ? m[1] : l)}</b></div>`
      + (m ? `<div class="lens-text">${esc(m[2])}</div>` : "") + "</li>";
  }).join("");
  $("fReqRow").hidden = !reqs.length;
  $("fReqs").innerHTML = reqs.map((r, i) => `<li><span class="rq">R${i + 1}</span><span>${esc(r)}</span></li>`).join("");
  // пока идёт заседание — сразу под вопросом; когда есть решение — ниже решения и обоснования
  const after = s.verdict ? $("whySec") : document.querySelector("#session .s-head");
  if (after.nextElementSibling !== $("frameSec")) after.after($("frameSec"));
}

function renderAttachments(s) {
  const box = $("sAtts");
  box.hidden = !s.attachments.length;
  if (!s.attachments.length) { box.innerHTML = ""; return; }
  const saved = !!s.done || !s.stage && !s.error && s.verdict;  // файлы доступны после сохранения заседания
  const html = s.attachments.map((a) => {
    const url = `/api/sessions/${encodeURIComponent(s.id)}/files/${encodeURIComponent(a.file)}`;
    const pic = a.kind === "image" && saved ? `<img src="${url}" alt="" loading="lazy">` : `<span class="ico">${esc(extIcon(a.name))}</span>`;
    const title = esc(a.name) + (a.warning ? ` — ${esc(a.warning)}` : "");
    return saved
      ? `<a class="s-att" href="${url}" target="_blank" rel="noopener" title="${title}">${pic}<span>${esc(a.name)}</span></a>`
      : `<span class="s-att" title="${title}">${pic}<span>${esc(a.name)}</span></span>`;
  });
  if (s.imageNotes && Object.keys(s.imageNotes).length) {
    html.push(`<button type="button" class="s-att" id="notesBtn" title="Текстовое описание картинок, которое получили модели без зрения"><span class="ico">Aa</span><span>Описание картинок</span></button>`);
  }
  box.innerHTML = html.join("");
}

$("sAtts").addEventListener("click", (e) => {
  if (!e.target.closest("#notesBtn") || !current) return;
  const notes = current.imageNotes || {};
  openModal({
    eyebrow: "Вложения", title: "Описание изображений",
    sub: `Сделал(а) ${esc(current.imageNotesBy || "модель")} для моделей, которые не видят картинки: ${esc((current.blind || []).join(", ") || "—")}`,
    tabs: [{ name: "Описание", html: Object.entries(notes).map(([n, t]) => `<h3>${esc(n)}</h3><div class="section">${md(t)}</div>`).join("") }],
  });
});

const PHASE_TEXT = {
  queued: "в очереди",
  sent: "запрос отправлен",
  thinking: "думает",
  writing: "пишет ответ",
  checking: "проверяю ответ",
  retry: "повтор",
  done: "готово",
  failed: "сбой",
};
const STALL_SEC = 40;

function phaseText(c) {
  let t = PHASE_TEXT[c.phase] || c.phase;
  if (c.phase === "thinking") t = c.rchars ? `рассуждает · ≈${approxTokens(c.rchars)} ток.` : "думает над ответом…";
  if (c.phase === "writing") t = `пишет ответ · ≈${approxTokens(c.chars)} ток.`;
  if (c.phase === "sent" && c.attempt > 1) t += ` (попытка ${c.attempt})`;
  if (c.phase === "retry") t = `повтор: ${c.detail || "временная ошибка"}`;
  if (c.phase === "done") t = `готово${c.cost != null ? ` · ${money(c.cost)}` : ""}`;
  if (c.phase === "failed") t = `сбой: ${c.detail || ""}`;
  return t;
}

function readyCount(s) {
  const names = Object.keys(s.work);
  return [names.filter((n) => s.work[n] !== "work"
    || (s.calls[n] && s.calls[n].stage === s.stage && ["done", "failed"].includes(s.calls[n].phase))).length, names.length];
}

// подписи под пройденными этапами — что получилось
function stageResult(s, key) {
  const n = Object.keys(s.proposals).length;
  if (s.mode === "research") {
    const q = (s.subq || []).length, l = (s.lenses || []).length, v = s.verdict || {};
    if (key === "framing" && q) return `${q} ${plural(q, "подвопрос", "подвопроса", "подвопросов")}${l ? `, ${l} ${plural(l, "угол", "угла", "углов")} зрения` : ""}`;
    if (key === "verdict" && s.verdict) return `известно ${(v.known || []).length} · спорно ${(v.contested || []).length} · неизвестно ${(v.unknown || []).length}`;
  }
  if (key === "framing") return s.criteria.length ? `${s.criteria.length} ${plural(s.criteria.length, "критерий", "критерия", "критериев")}${(s.requirements || []).length ? `, ${s.requirements.length} обязательных ${plural(s.requirements.length, "требование", "требования", "требований")}` : ""}` : "готово";
  if (key === "proposal") return n ? `${n} ${plural(n, "вариант", "варианта", "вариантов")}: ${Object.keys(s.proposals).sort().join(", ")}` : "готово";
  if (key === "critique") { const k = Object.keys(s.critiques).length; return k ? `${k} ${plural(k, "рецензия", "рецензии", "рецензий")}` : "готово"; }
  if (key === "revision") { const k = Object.keys(s.revised).length; return k ? `доработано: ${k}` : "готово"; }
  if (key === "vote") return s.tally && s.tally[0] ? `лидирует вариант ${s.tally[0].label}` : "готово";
  if (key === "factcheck") return s.facts ? "проверено" : "готово";
  if (key === "verdict") return s.verdict ? (s.verdict.acceptable === false ? "ни один вариант не подходит" : `выбран вариант ${s.verdict.winner}`) : "готово";
  return "готово";
}

function workerRows(s, now) {
  const rows = [];
  for (const name of Object.keys(s.work)) {
    const c = s.calls[name];
    if (!c || c.stage !== s.stage) {
      rows.push(`<li class="w idle"><span class="dot"></span><b>${esc(name)}</b><span class="w-phase">ожидает запуска</span><span class="w-time"></span></li>`);
      continue;
    }
    const active = PHASE_ACTIVE.has(c.phase);
    const secs = active ? (now - c.since) / 1000 : (c.final || 0);
    const cls = ["w", active ? "active" : "", c.phase].join(" ");
    rows.push(`<li class="${cls}"><span class="dot"></span><b>${esc(name)}</b><span class="w-phase">${esc(phaseText(c))}</span><span class="w-time" title="время этого обращения">${fmtSec(secs)}</span></li>`);
  }
  // председатель без своего варианта: показываем, почему его нет в списке
  if (["proposal", "revision"].includes(s.stage) && s.chairJudge && s.chair)
    rows.push(`<li class="w-note"><span class="w-note-ic" aria-hidden="true">⚖️</span><span><b>${esc(s.chair)}</b> — председатель. `
      + `Своего варианта не предлагает, чтобы не судить самого себя: оценивает варианты остальных и пишет итог.</span></li>`);
  return rows;
}

function renderActivity(s) {
  const finished = !!(s.done || s.error || s.paused);
  const now = Date.now();
  $("progressSec").hidden = !!s.done;
  $("cancelBtn").hidden = finished;
  const plan = stagePlan(s);
  const idx = plan.findIndex(([k]) => k === s.stage);
  const doneN = plan.filter(([k]) => s.doneStages.has(k)).length;

  // строка «что сейчас»
  const bar = $("stageBar").parentElement;
  bar.classList.remove("done", "stopped");
  if (s.paused) {
    const at = plan.find(([k]) => !s.doneStages.has(k));
    $("stageNow").innerHTML = `<b>Приостановлено${at ? ` на этапе «${esc(at[1])}»` : ""}</b> <span class="muted">· пройдено ${doneN} из ${plan.length} · можно продолжить</span>`;
    bar.classList.remove("indeterminate");
    bar.classList.add("stopped");
  } else if (s.error) {
    const at = plan.find(([k]) => !s.doneStages.has(k));
    $("stageNow").innerHTML = at ? `<b>Остановлено на этапе «${esc(at[1])}»</b> <span class="muted">· пройдено ${doneN} из ${plan.length}</span>` : "<b>Остановлено</b>";
    bar.classList.remove("indeterminate");
    bar.classList.add("stopped");
  } else if (s.saving) {
    $("stageNow").innerHTML = `<b>Готовлю отчёты</b> <span class="muted">· PDF, Word и Markdown</span>`;
    bar.classList.add("indeterminate");
    $("stageBar").style.width = "100%";
  } else if (!s.stage) {
    $("stageNow").innerHTML = `<b>Заседание запускается…</b> <span class="muted">подключаемся к моделям</span>`;
    bar.classList.add("indeterminate");
  } else {
    const [ready, total] = readyCount(s);
    const stageSec = s.stageStart ? (now - s.stageStart) / 1000 : 0;
    const who = ["framing", "verdict"].includes(s.stage) ? `председатель ${esc(s.chair || "")}` : total ? `готово ${ready} из ${total}` : "";
    $("stageNow").innerHTML = `<b>Этап ${idx + 1} из ${plan.length}: ${esc(STAGE_NAME[s.stage] || s.stage)}</b> <span class="muted">· ${who ? `${who} · ` : ""}${fmtSec(stageSec)}</span>`;
    bar.classList.toggle("indeterminate", false);
    const part = total ? ready / total : 0;
    $("stageBar").style.width = `${Math.max(4, Math.round(((doneN + part) / plan.length) * 100))}%`;
  }

  // этапы по порядку; у текущего — строки моделей
  const rows = !finished && s.stage && !s.saving ? workerRows(s, now) : [];
  $("timeline").innerHTML = plan.map(([key, name], i) => {
    const done = s.doneStages.has(key) && s.stage !== key;
    const active = s.stage === key && !finished;
    const cls = done ? "done" : active ? "active" : "";
    const num = done ? '<svg class="ic"><use href="#i-check"/></svg>' : String(i + 1);
    const round = active && (String(s.stageTitle[key] || "").match(/раунд (\d+)\/(\d+)/) || []);
    const sub = done ? stageResult(s, key) : active ? `${round[2] > 1 ? `раунд ${round[1]} из ${round[2]} — ` : ""}${stageHint(s, key)}` : "";
    return `<li class="${cls}"${active ? ' aria-current="step"' : ""}><span class="num">${num}</span><div><div class="st-name">${esc(name)}</div>`
      + `${sub ? `<div class="st-sub">${esc(sub)}</div>` : ""}${active && rows.length ? `<ul class="workers">${rows.join("")}</ul>` : ""}</div></li>`;
  }).join("");

  // подсказка, если модель долго молчит
  const slow = finished ? [] : Object.entries(s.calls).filter(([n, c]) => c.stage === s.stage && ["sent", "thinking"].includes(c.phase)
    && (now - c.since) / 1000 > STALL_SEC && s.work[n] === "work").map(([n]) => n);
  const connOk = now - s.lastMsg < 25000;
  $("activity").hidden = !slow.length;
  $("stallHint").hidden = !slow.length || !connOk;
  if (slow.length) {
    $("stallHint").textContent = `${slow.join(", ")} ${slow.length > 1 ? "думают" : "думает"} дольше ${STALL_SEC} с. `
      + "Это нормально: «думающие» модели рассуждают перед ответом до 1–3 минут. Связь с сервером есть, процесс не завис.";
  }

  // связь с сервером
  const conn = $("conn");
  if (finished) { conn.textContent = ""; conn.classList.remove("bad"); }
  else {
    const age = Math.round((now - s.lastMsg) / 1000);
    conn.classList.toggle("bad", !connOk);
    conn.textContent = connOk ? "связь есть" : `нет связи ${age} с — переподключаюсь…`;
  }

  // журнал (новые сверху)
  $("logCount").textContent = `${s.log.length} ${plural(s.log.length, "событие", "события", "событий")}${s.done ? ` · ${fmtSec(s.done.duration_s ?? 0)} · ${money(s.cost)}` : ""} ·`;
  if ($("logSec").open && $("log").dataset.n !== String(s.log.length)) {
    $("log").dataset.n = String(s.log.length);
    $("log").innerHTML = s.log.slice().reverse().map((e) =>
      `<li class="${e.kind}"><time>${e.t.toLocaleTimeString("ru-RU")}</time>${esc(e.text)}</li>`).join("");
  }
}
$("logSec").addEventListener("toggle", () => { if ($("logSec").open && current) { $("log").dataset.n = ""; renderActivity(current); } });

const IDEA_BADGE = { included: ["ok", "Включена"], rejected: ["bad", "Отклонена"], not_addressed: ["plain", "Не рассмотрена"] };
// согласие моделей — не подтверждение фактов: нейтральная метка, основания вывода — отдельной
const AGREE_BADGE = {
  high: ["plain", "Модели согласны с выбором"],
  medium: ["warn", "Модели частично расходятся"],
  low: ["bad", "Модели разошлись"],
};
const AGREE_NAME = { high: "высокое", medium: "среднее", low: "низкое" };
const AGREE_HINT = {
  high: "модели в основном одинаково упорядочили варианты (это согласие, а не гарантия правоты)",
  medium: "модели частично расходятся в оценке вариантов",
  low: "мнения совета разошлись — относитесь к решению осторожно и посмотрите особые мнения",
};

function renderVerdict(s) {
  const v = s.verdict;
  $("verdictSec").hidden = !v;
  $("whySec").hidden = !v;
  if (!v) return;
  const p = s.proposals[v.winner];
  $("vTitle").textContent = s.mode === "research" ? "Итог исследования" : (p && p.title ? p.title : `Вариант ${v.winner}`);
  const row = s.tally && s.tally.find((r) => r.label === v.winner);
  const firsts = row && row.ballots ? `${row.first_places} из ${row.ballots} ${plural(row.ballots, "голоса", "голосов", "голосов")} за первое место` : "";
  if (s.mode === "research") {
    const L = s.ledger && s.ledger.claims ? s.ledger.claims.length : 0;
    $("vSub").textContent = [`ответ по ${(s.subq || []).length || "—"} подвопросам`, L ? "" : "поиск по источникам не проводился"].filter(Boolean).join(" · ");
    $("vSub").title = `общий каркас итога — вариант ${v.winner}`;
  } else if (v.acceptable === false) {
    $("vTitle").textContent = "Ни один вариант не подходит";
    $("vSub").textContent = [`лидер сравнения — вариант ${v.winner}${p && p.title ? ` «${p.title}»` : ""}, но он не рекомендован`, firsts].filter(Boolean).join(" · ");
  } else $("vSub").textContent = [`Вариант ${v.winner}`, s.authors[v.winner] != null ? (authorOf(s, v.winner) === "автор скрыт" ? "автор скрыт" : `предложил ${authorOf(s, v.winner)}`) : "ваш вариант", firsts].filter(Boolean).join(" · ");

  const lead = s.tally && s.tally[0] && s.tally[0].label;
  const tie = (s.coLeaders || []).length > 1;
  const deviated = lead && v.winner && v.winner !== lead && !(s.coLeaders || []).includes(v.winner);
  const badges = [];
  if (s.agreement) { const [k, t] = AGREE_BADGE[s.agreement.level]; badges.push(`<span class="badge ${k}" title="${esc(AGREE_HINT[s.agreement.level])}">${t}</span>`); }
  if (tie) badges.push(`<span class="badge warn">Ничья за первое место</span>`);
  if (v.acceptable === false) badges.push(`<span class="badge bad" title="${esc((v.decisive_defects || []).join("; "))}">Ни один вариант не подходит</span>`);
  if (deviated) badges.push(`<span class="badge bad">Председатель выбрал не лидера</span>`);
  if (s.facts && Object.values(s.facts).some((r) => (r.claims || []).some((c) => c.status === "refuted"))) badges.push(`<span class="badge bad">Есть опровергнутые факты</span>`);
  const basis = basisBadge(s);
  if (basis) badges.unshift(basis);
  if (s.mock) badges.push(`<span class="badge plain">демо-режим</span>`);
  $("vBadges").innerHTML = badges.join("");

  const warns = [];
  if (s.agreement && s.agreement.level === "low") warns.push(`Мнения совета разошлись (τ = ${s.agreement.tau.toFixed(2)}): ${AGREE_HINT.low}.`);
  if (deviated) warns.push(`Лидер голосования — вариант ${lead}, но председатель выбрал ${v.winner}.${(v.deviation || v.why) ? ` Причина: ${v.deviation || v.why}` : ""}`);
  if (tie) warns.push(`Варианты ${s.coLeaders.join(", ")} набрали одинаково — по голосам их не различить${s.coLeaders.includes(v.winner) ? `; председатель выбрал ${v.winner}` : ""}.`);
  $("vAgreeWarn").hidden = !warns.length;
  $("vAgreeWarn").innerHTML = warns.map(esc).join("<br>");
  $("vAnswer").innerHTML = md(v.final_answer);
  renderResearch(s);

  $("vFacts").hidden = !s.facts || !Object.keys(s.facts).length;
  $("vFacts").innerHTML = s.facts ? factsHtml(s) : "";
  $("vWhy").innerHTML = md(v.why);
  $("vDissentBox").hidden = !(v.dissent || []).length;
  $("vDissent").innerHTML = (v.dissent || []).map((x) => `<li>${inline(x)}</li>`).join("");
  $("vRisksBox").hidden = !(v.risks || []).length;
  $("vRisks").innerHTML = (v.risks || []).map((x) => `<li>${inline(x)}</li>`).join("");
  const ideas = s.ideas || [];
  $("vIdeasBox").hidden = !ideas.length;
  if (ideas.length) {
    const cnt = (k) => ideas.filter((i) => i.decision === k).length;
    const na = cnt("not_addressed");
    $("vIdeasSum").textContent = `Критики отметили, что стоит сохранить из вариантов, не ставших основой решения. `
      + `Председатель решил по каждой идее: включено ${cnt("included")}, отклонено ${cnt("rejected")}${na ? `, не рассмотрено ${na}` : ""}.`;
    $("vIdeas").innerHTML = ideas.map((i) => {
      const [k, t] = IDEA_BADGE[i.decision] || IDEA_BADGE.not_addressed;
      const reason = i.reason || (i.decision === "not_addressed" ? "председатель не ответил по этой идее" : "");
      return `<li><span class="badge ${k}">${t}</span> <span class="idea-text">${inline(i.idea)}</span>`
        + `<div class="muted small">вариант ${esc(i.label || "")}${reason ? ` · ${inline(reason)}` : ""}</div></li>`;
    }).join("");
  }
  const nr = (v.risks || []).length, nd = (v.dissent || []).length;
  $("whySum").textContent = ["обоснование", nr ? `${nr} ${plural(nr, "риск", "риска", "рисков")}` : "", nd ? `${nd} ${plural(nd, "особое мнение", "особых мнения", "особых мнений")}` : "",
    s.facts && Object.keys(s.facts).length ? "проверка фактов" : "",
    ideas.length ? `${ideas.length} ${plural(ideas.length, "идея", "идеи", "идей")} из других вариантов` : ""].filter(Boolean).join(" · ");
  if (s.done && $("downloads").dataset.id !== s.id) { $("downloads").dataset.id = s.id; $("downloads").innerHTML = downloadLinks(s.id); }
}

const TIE_NAME = { borda: "балл Борда", pairwise: "попарный счёт", critique: "оценка критики", label: "жребий (метки случайны)" };
const POSITION_NAME = { kept: "позиция сохранена", improved: "доработан в рамках своего подхода", switched: "подход сменён" };
const FLAG_NAME = { against: "порядок почти обратный общему мнению",
  leader_last: "ставит на последнее место вариант, который остальные считают лучшим" };
const FACT_ICON = { confirmed: "✅", refuted: "❌", unclear: "❔", user_doc: "📄" };
const FACT_NAME = { confirmed: "подтверждено", refuted: "опровергнуто", unclear: "не удалось проверить", user_doc: "из документов пользователя" };

function coverageHtml(s) {
  let out = "";
  const c = s.coverage;
  if (c) {
    if (c.options < 3) out += `<br>⚠️ Вариантов меньше трёх — голосование почти ничего не решает, по сути выбор делает председатель.`;
    else out += `<br>Каждая пара вариантов сравнивалась ${c.min === c.max ? c.min : `от ${c.min} до ${c.max}`} раз${c.min < 2 ? " — мало данных, относитесь к порядку осторожно" : ""}.`;
  }
  const d = Object.entries(s.decisive || {});
  if (d.length) out += `<br><b>Решающие голоса</b> (лидер меняется без одного бюллетеня): ${d.map(([v, a]) => `без ${esc(v)} лидером был бы ${esc(a)}`).join("; ")}.`;
  return out;
}

function positionsHtml(s) {
  const c = { kept: 0, improved: 0, switched: 0 };
  for (const [l, p] of Object.entries(s.proposals || {})) if (s.revised[l] && p.comparison !== undefined && p.position in c) c[p.position]++;
  const n = c.kept + c.improved + c.switched;
  if (!n) return "";
  return `<br>После доработки: сохранили позицию — ${c.kept}, доработали свой подход — ${c.improved}, сменили подход — ${c.switched}`
    + (n >= 2 && c.switched * 2 >= n ? ". <b>Половина и больше сменили подход — варианты могли сблизиться, смотрите особые мнения.</b>" : ".");
}

function flagsHtml(s) {
  const f = Object.entries(s.ballotFlags || {});
  if (!f.length) return "";
  const leader = s.tally && s.tally[0] && s.tally[0].label;
  const robust = s.leaderWithout && s.leaderWithout !== leader
    ? `<b>Без них лидер был бы ${esc(s.leaderWithout)}</b> — победа зависит от спорных голосов.`
    : "Без них лидер тот же — результат устойчив.";
  return `<div class="flags">⚠️ <b>Бюллетени против общего мнения</b> (голоса учтены, это только пометка):`
    + f.map(([n, x]) => `<br>• ${esc(n)} — ${x.flags.map((k) => FLAG_NAME[k] || k).join("; ")}${x.tau != null ? ` (τ = ${x.tau.toFixed(2)})` : ""}`).join("")
    + `<br>${robust}</div>`;
}

function factsHtml(s) {
  const safeUrl = (u) => /^https?:\/\//i.test(u) ? u : "";
  return `<h3>Проверка фактов <span class="muted small">(${esc(s.factModel || "")}, поиск в интернете)</span></h3>`
    + Object.entries(s.facts).map(([label, r]) => `<div class="fact-block"><b>Вариант ${esc(label)}</b>${
      (s.proposals[label] || {}).title ? ` — ${esc(s.proposals[label].title)}` : ""}${r.summary ? `<div class="muted small">${inline(r.summary)}</div>` : ""}
      <ul>${(r.claims || []).map((c) => `<li class="fact-${esc(c.status)}">${FACT_ICON[c.status] || "❔"} <b>${FACT_NAME[c.status] || esc(c.status)}:</b> ${inline(c.claim)}${
        c.note ? `<div class="muted small">${inline(c.note)}</div>` : ""}${(c.sources || []).map(safeUrl).filter(Boolean).length
        ? `<div class="small">${(c.sources || []).map(safeUrl).filter(Boolean).slice(0, 3).map((u, i) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">источник ${i + 1}</a>`).join(" · ")}</div>` : ""}</li>`).join("")}</ul></div>`).join("");
}

// главное о голосовании одной фразой — без терминов
function tallyLead(s) {
  const t = s.tally;
  if (!t || !t.length) return "";
  const top = t[0];
  const n = t.length;
  const nb = top.ballots || 0;
  let txt = `Модели ранжировали ${n} ${plural(n, "вариант", "варианта", "вариантов")}, не зная авторов. `;
  if ((s.coLeaders || []).length > 1) txt += `Первое место поделили варианты ${s.coLeaders.join(", ")}.`;
  else if (nb) txt += `Вариант ${top.label} поставили первым ${top.first_places} из ${nb}${top.schulze != null && top.schulze === n - 1 ? "; он выигрывает у каждого соперника один на один" : ""}.`;
  if (s.agreement) txt += ` ${AGREE_BADGE[s.agreement.level][1]}.`;
  return txt;
}

function renderTally(s) {
  const ballots = Object.entries(s.ballots);
  $("tallySec").hidden = !s.tally && !ballots.length;
  const win = s.verdict && s.verdict.winner;
  if (s.tally) {
    $("tallyLead").textContent = tallyLead(s);
    const max = Math.max(...s.tally.map((r) => r.borda), 0.0001);
    $("tallyBars").innerHTML = s.tally.map((r, i) => `<div class="tbar ${r.label === win ? "win" : ""}">
      <span class="lbl">${esc(r.label)}</span>
      <span class="t-title">${esc((s.proposals[r.label] || {}).title || "")} <small>· ${esc(authorOf(s, r.label))}</small></span>
      <span class="t-place">${i + 1} место${r.label === win ? " · решение" : ""}</span>
      <span class="track" role="img" aria-label="Балл ${r.borda.toFixed(2)} из 1"><i style="width:${Math.max(3, (r.borda / max) * 100).toFixed(0)}%"></i></span></div>`).join("");
    const sch = s.tally[0] && s.tally[0].schulze != null;
    $("tally").innerHTML = `<thead><tr><th>#</th><th>Вариант</th><th>Автор</th>${sch ? '<th title="Метод Шульце: скольких соперников вариант побеждает в прямых сравнениях — определяет порядок">Шульце</th>' : ""}<th>Борда</th><th title="Попарные сравнения: побед минус поражений">Попарно</th><th>Критика</th><th>1-х мест</th></tr></thead><tbody>${
      s.tally.map((r, i) => `<tr class="${r.label === win ? "winner" : ""}">
        <td class="num">${i + 1}</td>
        <td><span class="lbl">${esc(r.label)}</span></td>
        <td>${esc(authorOf(s, r.label))}</td>
        ${sch ? `<td class="num">${r.schulze} из ${s.tally.length - 1}</td>` : ""}
        <td class="num">${r.borda.toFixed(2)}</td>
        <td class="num">${r.pairwise == null ? "—" : (r.pairwise > 0 ? "+" : "") + r.pairwise}</td>
        <td class="num">${r.avg_score == null ? "—" : r.avg_score.toFixed(1)}</td>
        <td class="num">${r.first_places}/${r.ballots}</td></tr>`).join("")}</tbody>`;
    const ties = s.tally.slice(0, -1).map((r, i) => r.tie_break
      ? `${esc(r.label)} выше ${esc(s.tally[i + 1].label)}: при равенстве ${sch ? "по Шульце" : "по баллу Борда"} решило ${TIE_NAME[r.tie_break] || r.tie_break}` : "").filter(Boolean);
    const a = s.agreement;
    $("tallyNote").innerHTML = (a ? `<b>Согласие совета: ${AGREE_NAME[a.level]}</b> (τ = ${a.tau.toFixed(2)}) — ${AGREE_HINT[a.level]}.<br>` : "")
      + (sch ? "Порядок: метод Шульце — кто выигрывает в прямых сравнениях; при равенстве — балл Борда (0–1, среднее место в бюллетенях), затем попарный счёт, затем жребий. Оценка критики — для сведения: она дана версиям до доработки."
        : "Порядок: балл Борда; при равенстве — попарное сравнение, затем оценка критики.")
      + ties.map((t) => `<br>• ${t}`).join("") + coverageHtml(s) + positionsHtml(s) + flagsHtml(s);
  } else {
    $("tallyLead").textContent = ballots.length ? `Голосуют модели: ${ballots.length} ${plural(ballots.length, "бюллетень", "бюллетеня", "бюллетеней")} получено.` : "";
    $("tallyBars").innerHTML = "";
    $("tally").innerHTML = "";
    $("tallyNote").innerHTML = "";
  }
  $("ballots").innerHTML = ballots.length ? `<h3>Бюллетени</h3>` + ballots.map(([voter, b]) => `<div class="ballot"><b>${esc(voter)}</b>
    <span class="rank">${b.ranking.map(esc).join(" › ")}</span>${b.rationale ? `<span class="why">${inline(b.rationale)}</span>` : ""}</div>`).join("") : "";
}

function renderProposals(s) {
  const labels = Object.keys(s.proposals).sort();
  $("proposalsSec").hidden = !labels.length;
  const win = s.verdict && s.verdict.winner;
  $("proposals").innerHTML = labels.map((l) => {
    const p = s.proposals[l];
    const list = (name, xs) => (xs && xs.length ? `<div><b>${name}:</b> ${xs.map(inline).join("; ")}</div>` : "");
    return `<article class="proposal ${l === win ? "win" : ""}">
      <header><span class="lbl">${esc(l)}</span><div><h3 class="clickable" data-full="${esc(l)}">${esc(String(p.title || "").replace(/\s*\((доработ\w*|после доработки)\)\s*$/i, ""))}${s.revised[l] ? '<span class="tag">доработан</span>' : ""}${l === win ? ' <span class="tag dec">решение</span>' : ""}</h3>
      <div class="author">${esc(authorOf(s, l))}</div></div></header>
      <div class="body md">${md(p.summary)}</div>
      <button class="open-full" type="button" data-full="${esc(l)}"><svg class="ic" aria-hidden="true" style="width:16px;height:16px"><use href="#i-expand"/></svg>Открыть полный ответ</button>
      <div class="lists">${list("Плюсы", p.pros)}${list("Риски", p.risks)}</div>
      ${p.changes ? `<div class="changes"><b>Что изменено${s.revised[l] && POSITION_NAME[p.position] && p.comparison !== undefined ? ` (${POSITION_NAME[p.position]})` : ""}:</b> ${inline(p.changes)}</div>` : ""}
    </article>`;
  }).join("");
  for (const body of $("proposals").querySelectorAll(".body")) {
    if (body.scrollHeight <= body.clientHeight + 2) body.classList.add("short");
  }
}

$("proposals").addEventListener("click", (e) => {
  const b = e.target.closest("[data-full]");
  if (b && current) openProposal(current, b.dataset.full);
});

function renderMatrix(s) {
  const reviewers = Object.keys(s.critiques);
  $("critiqueSec").hidden = !reviewers.length;
  if (!reviewers.length) { $("matrix").innerHTML = ""; return; }
  const labels = Object.keys(s.proposals).sort();
  const avg = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const colSums = Object.fromEntries(labels.map((l) => [l, []]));
  const rows = reviewers.map((rv) => {
    const byLabel = Object.fromEntries(s.critiques[rv].reviews.map((r) => [r.label, r]));
    const cells = labels.map((l) => {
      const r = byLabel[l];
      if (!r) return `<td class="self">${s.authors[l] === rv ? "свой" : "—"}</td>`;
      const v = avg(Object.values(r.scores));
      colSums[l].push(v);
      return `<td class="cell" style="${scoreColor(v)}"><button type="button" data-reviewer="${esc(rv)}" data-label="${esc(l)}" aria-label="${esc(rv)} о варианте ${esc(l)}: ${v.toFixed(1)}. Открыть отзыв">${v.toFixed(1)}</button></td>`;
    }).join("");
    return `<tr><th scope="row" class="clickable" data-reviewer="${esc(rv)}" tabindex="0" title="Открыть все отзывы">${esc(rv)}</th>${cells}</tr>`;
  }).join("");
  const foot = labels.map((l) => {
    if (!colSums[l].length) return "<td></td>";
    const v = avg(colSums[l]);
    return `<td class="num" style="text-align:center;font-weight:700">${v.toFixed(1)}</td>`;
  }).join("");
  $("matrix").innerHTML = `<thead><tr><th>Рецензент</th>${labels.map((l) => `<th style="text-align:center">${esc(l)}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody><tfoot><tr><th scope="row">Среднее</th>${foot}</tr></tfoot>`;
}

function tick() {
  if (!current) return;
  const secs = current.done && current.done.duration_s != null
    ? current.done.duration_s
    : Math.round((Date.now() - current.startedAt) / 1000);
  $("sTime").textContent = secs >= 60 ? `${Math.floor(secs / 60)} мин ${Math.round(secs % 60)} с` : `${Math.round(secs)} с`;
  if (view === "session") renderActivity(current);
}

/* ------------------------------------------------------------ поток событий */

function closeStream() {
  if (stream) { stream.close(); stream = null; }
  if (timer) { clearInterval(timer); timer = null; }
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; if (current) { render(current); tick(); markAllScroll($("session")); } });
}

function openSession(id) {
  closeStream();
  current = newState(id);
  const s = current;
  showView("session");
  // адрес заседания (в том числе с открытым вариантом /p/X) уже стоит — новый шаг истории не нужен
  if (!sameHash(location.hash.replace(/\/p\/[A-Z]$/, ""), `#/s/${encodeURIComponent(id)}`)) go(`#/s/${encodeURIComponent(id)}`);
  markActiveHistory();
  render(s);
  window.scrollTo({ top: 0 });
  timer = setInterval(tick, 1000);
  stream = new EventSource(`/api/sessions/${encodeURIComponent(id)}/events`);
  stream.onmessage = (msg) => {
    if (current !== s) return;
    const ev = JSON.parse(msg.data);
    s.lastMsg = Date.now();
    apply(s, ev);
    if (ev.type === "ping") return;
    scheduleRender();
    if (pendingProposal && ev.type === "done") { const l = pendingProposal; pendingProposal = null; setTimeout(() => openProposal(s, l), 50); }
    if (ev.type === "done" || ev.type === "error" || ev.type === "paused") {
      // уведомляем только о заседании, которое шло у нас на глазах (не о повторном открытии из архива)
      if (s.at >= s.openedAt - 2000 && s.at - s.startedAt > 5000) notifyFinished(s, ev.type === "done");
      closeStream();
      scheduleRender();
      loadHistory();
    }
  };
  stream.onerror = () => {
    // браузер переподключится сам и продолжит с Last-Event-ID; если заседание уже не найдено — закрываем
    if (stream && stream.readyState === EventSource.CLOSED) {
      if (!s.done && !s.error) { s.error = "соединение с сервером потеряно или заседание не найдено"; scheduleRender(); }
      closeStream();
    }
  };
}

function queryToggle(finished) {
  const b = $("sQueryBody");
  $("sQueryToggle").hidden = !finished && !b.dataset.clipped;
  $("sQueryToggle").textContent = b.classList.contains("open") ? "Свернуть вопрос" : finished ? "Исходный вопрос ▾" : "Показать вопрос полностью";
}
$("sQueryToggle").addEventListener("click", () => {
  $("sQueryBody").classList.toggle("open");
  queryToggle($("sQuery").classList.contains("folded"));
});

$("cancelBtn").addEventListener("click", async () => {
  if (!current) return;
  const ok = await ask({ title: "Поставить на паузу?", html: "<p>Пауза наступит, как только вернутся уже отправленные запросы. Полученные ответы сохранятся — продолжить можно позже, не оплачивая их повторно.</p><p class=\"muted small\">Если продолжать не нужно — после паузы нажмите «Удалить».</p>", ok: "Пауза", cancel: "Продолжать работу" });
  if (!ok) return;
  try { await api(`/api/sessions/${encodeURIComponent(current.id)}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
});
$("blind").addEventListener("change", () => current && render(current));
$("budgetBtn").addEventListener("click", raiseBudget);
$("redoBtn").addEventListener("click", async () => {
  const s = current;
  if (!s) return;
  const suggest = Math.ceil((Number(s.cost || 0) + Math.max(1, Number(config.budget || 1))) * 2) / 2;
  const ok = await ask({ title: "Доделать заседание?", ok: "Доделать",
    html: `<p>Ответы, которые модели уже дали (и за которые заплачено), возьмутся из сохранённого. Заново спросим только `
      + `шаги со сбоями и всё, что от них зависит (например, голосование и итог). Новый итог заменит этот.</p>`
      + `<p class="muted small">Если сбой был из-за лимита ключа или баланса OpenRouter — сначала поднимите лимит или пополните баланс.</p>`
      + `<label class="field"><span>Бюджет заседания, $ (уже потрачено ${money(s.cost)})</span><input id="redoBudget" type="number" min="0.1" step="0.5" value="${suggest}" inputmode="decimal"></label>` });
  if (!ok) return;
  const budget = Number((document.getElementById("redoBudget") || {}).value) || suggest;
  try {
    await api(`/api/sessions/${encodeURIComponent(s.id)}/redo`, { method: "POST", body: { budget } });
    openSession(s.id);
  } catch (e) { toast(e.message); }
});
$("sBudgetWarn").addEventListener("click", (e) => { if (e.target.closest("[data-raise]")) raiseBudget(); });
$("pResume").addEventListener("click", () => current && resumeSession(current.id));
$("pDrop").addEventListener("click", async () => {
  if (!current) return;
  const ok = await ask({ title: "Удалить приостановленное заседание?", danger: true, ok: "Удалить", cancel: "Оставить",
    html: "<p>Продолжить его будет нельзя. Уже потраченные деньги не вернутся.</p>" });
  if (!ok) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(current.id)}/paused`, { method: "DELETE" });
    loadHistory(); goHome();
  } catch (e) { toast(e.message); }
});

/* ------------------------------------------------------------ голосовой ввод */

let recorder = null, recChunks = [], recStart = 0, recTimer = null;
const MAX_REC_SEC = 300;
// микрофон доступен только по HTTPS или на localhost
if (navigator.mediaDevices && window.MediaRecorder && window.isSecureContext) $("micBtn").hidden = false;

function micLabel() {
  const s = Math.floor((Date.now() - recStart) / 1000);
  $("micBtn").textContent = `⏹ ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  if (s >= MAX_REC_SEC) stopRecording();
}

async function startRecording() {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { showFormError("Нет доступа к микрофону — разрешите его в настройках браузера"); return; }
  recChunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => e.data.size && recChunks.push(e.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(recTimer);
    const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
    $("micBtn").classList.remove("rec"); $("micBtn").classList.add("busy"); $("micBtn").textContent = "Распознаю…";
    try {
      const res = await fetch(`/api/transcribe${$("mock").checked ? "?mock=1" : ""}`, { method: "POST", body: blob,
        headers: { "Content-Type": blob.type || "application/octet-stream" } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const q = $("question");
      q.value = q.value.trim() ? `${q.value.trim()}\n${data.text}` : data.text;
      q.focus();
      showFormError("");
      $("estimate").hidden = true;
    } catch (e) {
      showFormError(`Не удалось распознать речь: ${e.message}`);
    } finally {
      $("micBtn").classList.remove("busy"); $("micBtn").textContent = "🎤"; recorder = null;
    }
  };
  recorder.start();
  recStart = Date.now();
  $("micBtn").classList.add("rec");
  micLabel();
  recTimer = setInterval(micLabel, 500);
}

function stopRecording() { if (recorder && recorder.state === "recording") recorder.stop(); }

$("micBtn").addEventListener("click", (e) => {
  e.preventDefault();
  if ($("micBtn").classList.contains("busy")) return;
  if (recorder) stopRecording(); else startRecording();
});

/* ------------------------------------------------------------ статистика */

async function openStats() {
  closeStream();
  current = null;
  go("#/stats");
  showView("stats");
  markActiveHistory();
  let d;
  try { d = await api("/api/stats"); } catch (e) { $("statsBody").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!d.sessions) { $("statsBody").innerHTML = '<div class="arc-empty">Пока нет заседаний — статистика появится после первого.</div>'; return; }
  const dur = (s) => (s >= 60 ? `${Math.floor(s / 60)} мин ${Math.round(s % 60)} с` : `${Math.round(s)} с`);
  const kpi = (v, l) => `<div class="kpi"><div class="kv">${v}</div><div class="kl">${l}</div></div>`;
  const maxCost = Math.max(...d.months.map((m) => m.cost), 0.0001);
  const monthName = (m) => { const [y, mm] = m.split("-"); return new Date(+y, +mm - 1, 1).toLocaleDateString("ru-RU", { month: "short", year: "2-digit" }); };
  const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  $("statsBody").innerHTML = `
    <div class="kpis">
      ${kpi(d.sessions, "заседаний")}
      ${kpi(money(d.total_cost), "потрачено всего")}
      ${kpi(money(d.month_cost), `в этом месяце (${d.month_sessions})`)}
      ${kpi(money(d.avg_cost), "средняя цена заседания")}
      ${kpi(dur(d.avg_duration), "среднее время")}
      ${kpi(d.with_files, "с файлами")}
      ${kpi(d.followups, "уточнений")}
    </div>
    <div class="stats-grid">
      <div>
        <h2>Расходы по месяцам</h2>
        <div class="mbars">${d.months.map((m) => `<div class="mbar"><span>${esc(monthName(m.month))}</span>
          <div class="track" title="${m.sessions} заседаний"><i style="width:${Math.max(2, (m.cost / maxCost) * 100)}%"></i></div>
          <span class="val">${money(m.cost)}</span></div>`).join("")}</div>
        ${d.fact_check && d.fact_check.sessions ? `<h2 style="margin-top:1.2rem">Проверка фактов</h2>
        <p>Заседаний с проверкой: <b>${d.fact_check.sessions}</b> · ✅ ${d.fact_check.confirmed} подтверждено ·
          ❌ ${d.fact_check.refuted} опровергнуто · ❔ ${d.fact_check.unclear} не удалось проверить</p>` : ""}
        <h2 style="margin-top:1.2rem">Режимы</h2>
        <div class="chips">${d.modes.map((m) => `<span class="chip">${esc(m.name)}: ${m.count}</span>`).join("")}</div>
      </div>
      <div>
        <h2>Модели</h2>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Участник</th><th>Заседаний</th><th title="Сколько её вариантов совет выбрал решением — из предложенных ею">Выбрано лучшим</th><th>Доля</th><th>Ср. оценка</th><th>Сбоев</th><th>Расходы</th></tr></thead>
          <tbody>${d.models.map((m) => `<tr>
            <td><b>${esc(m.name)}</b><div class="muted small">${esc(m.model)}</div></td>
            <td class="num">${m.sessions}</td>
            <td class="num">${m.proposals ? `${m.wins} из ${m.proposals}` : '<span class="muted" title="Своих вариантов не писала — например, была председателем">не предлагала</span>'}</td>
            <td>${m.proposals ? `<div class="win-cell"><div class="bar"><i style="width:${Math.round((m.win_rate || 0) * 100)}%"></i></div>${pct(m.win_rate)}</div>` : '<span class="muted">—</span>'}</td>
            <td class="num">${m.avg_score == null ? "—" : m.avg_score.toFixed(1)}</td>
            <td class="num" style="${m.failures ? "color:var(--bad)" : ""}">${m.failures}</td>
            <td class="num" title="в среднем ${money(m.cost_per_session)} за заседание">${money(m.cost)}</td></tr>`).join("")}</tbody>
        </table></div>
        <p class="muted small">«Выбрано лучшим» — сколько вариантов модели совет выбрал решением в <b>ваших</b> заседаниях; на малом числе заседаний это не рейтинг моделей вообще.
          «Не предлагала» — своих вариантов не было (обычно председатель). «Ни один не подходит» победой не считается.
          Средняя оценка — как её варианты оценивали другие модели (1–10). Сбои — пустые ответы, ошибки формата и связи.</p>
        ${(d.chairs || []).length ? `<h2 style="margin-top:1.2rem">Председатели</h2>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Председатель</th><th>Заседаний</th><th>Выбрал не лидера</th><th>Доля</th><th>Ср. согласие</th></tr></thead>
          <tbody>${d.chairs.map((c) => `<tr>
            <td><b>${esc(c.name)}</b>${c.fallbacks ? `<div class="muted small">не ответил: ${c.fallbacks}</div>` : ""}</td>
            <td class="num">${c.sessions}</td>
            <td class="num" title="${esc((c.examples || []).map((e) => `${e.title}: лидер ${e.leader}, выбран ${e.winner}`).join("\n"))}">${c.overrides} из ${c.decisions}</td>
            <td class="num">${pct(c.override_rate)}</td>
            <td class="num">${c.avg_agreement == null ? "—" : c.avg_agreement.toFixed(2)}</td></tr>`).join("")}</tbody>
        </table></div>
        <p class="muted small">«Выбрал не лидера» — председатель назвал победителем не первый вариант голосования
          (он обязан это обосновать). Изредка — нормально; часто — председатель своеволен, стоит сменить модель.
          Согласие — насколько одинаково модели ранжировали варианты (τ: 1 — полностью, 0 — никак).</p>` : ""}
      </div>
    </div>
    ${(d.stages || []).length ? `<h2 style="margin-top:1.4rem">Куда уходят деньги</h2>
      <p class="muted small">По всем заседаниям: доля каждого этапа в расходах, повторы и продолжения, вход из кэша.</p>
      ${costTable(d.stages)}` : ""}
    ${pilotHtml(d)}`;
}
$("statsBtn").addEventListener("click", async () => { await openStats(); markAllScroll($("statsBody")); });

// пилот «исследование против обсуждения»: показатели исследований и тот же вопрос в разных режимах
function pilotHtml(d) {
  const rows = d.research || [];
  if (!rows.length) return "";
  const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const stars = (r) => (r ? "★".repeat(r) + "☆".repeat(5 - r) : '<span class="muted">—</span>');
  const links = (r) => (r.links_checked ? `${r.links_ok} из ${r.links_checked}${r.links_dead ? ` <span class="bad-text">· мёртвых ${r.links_dead}</span>` : ""}` : "—");
  const runRow = (r) => `<tr>
      <td class="pilot-title"><a href="#/s/${encodeURIComponent(r.id)}" title="${esc(r.title)}">${esc(shortTitle(r.title, 80))}</a><div class="muted small">${esc(r.date)} · ${esc(MODE_NAME[r.mode] || r.mode)}</div></td>
      <td class="num">${r.checked ? `${r.confirmed} из ${r.checked}` : "—"}<div class="muted small">${pct(r.confirmed_share)}</div></td>
      <td class="num">${r.known == null ? "—" : `${r.known} · ${r.contested} · ${r.unknown}`}</td>
      <td class="num">${links(r)}</td>
      <td class="num">${r.subq ? `${r.covered} из ${r.subq}${r.partial ? ` <span class="muted small">(+${r.partial} частично)</span>` : ""}` : "—"}</td>
      <td class="num">${money(r.cost)}</td>
      <td class="num" style="${r.failures ? "color:var(--bad)" : ""}">${r.failures}</td>
      <td class="stars-cell" title="${esc(r.note || "")}">${stars(r.rating)}</td><td class="stars-cell">${stars(r.solo_rating)}</td></tr>`;
  const head = `<thead><tr><th>Заседание</th><th>Подтверждено</th><th>Известно · спорно · неизвестно</th><th>Живые ссылки</th>
      <th>Подвопросы раскрыты</th><th>Цена</th><th>Сбои</th><th>Ваша оценка</th><th title="оценка ответа одной модели с поиском на тот же вопрос">Одна модель</th></tr></thead>`;
  const pairs = (d.pairs || []).map((p) => `<h3 class="pair-title">${esc(p.title)}</h3>
      <div class="table-wrap"><table class="table pilot">${head}<tbody>${p.runs.map(runRow).join("")}</tbody></table></div>`).join("");
  return `<section class="pilot-sec"><h2 style="margin-top:1.4rem">Пилот: исследования</h2>
    <p class="muted small">Подтверждено — сколько проверенных поиском утверждений источники подтвердили. Живые ссылки —
      сколько ссылок реестра открылось при проверке. Оценку пользы ставьте под решением каждого заседания (★ 1–5):
      по ней видно, окупается ли исследование.</p>
    <div class="table-wrap"><table class="table pilot">${head}<tbody>${rows.slice().reverse().map(runRow).join("")}</tbody></table></div>
    ${pairs ? `<h2 style="margin-top:1.2rem">Один вопрос — разные режимы</h2>
      <p class="muted small">Заседания с тем же текстом вопроса в разных режимах — для прямого сравнения.</p>${pairs}`
      : '<p class="muted small">Чтобы сравнить режимы, задайте тот же вопрос в «Подробном обсуждении» и в «Исследовании» — кнопка «Повторить» под решением.</p>'}</section>`;
}

// пилот: ответ одной модели с поиском (контрольная группа) и переигранный арбитраж
async function loadPilot(s) {
  let p;
  try { p = await api(`/api/sessions/${encodeURIComponent(s.id)}/pilot`); } catch { return; }
  $("pilotSec").hidden = false;
  renderPilot(s, p);
}

function renderPilot(s, p) {
  const solo = p.solo, rep = p.replay;
  const row = (k, a, b) => `<tr><td>${k}</td><td class="num">${a ?? "—"}</td><td class="num">${b ?? "—"}</td></tr>`;
  const stars = (val) => [1, 2, 3, 4, 5].map((n) => `<button type="button" class="star${n <= (val || 0) ? " on" : ""}" data-srate="${n}" aria-label="${n} из 5">${n <= (val || 0) ? "★" : "☆"}</button>`).join("");
  $("pilotSum").textContent = [solo ? "есть ответ одной модели" : "", rep ? `арбитраж: подтверждено ${rep.after.confirmed} из ${rep.after.checked}` : ""].filter(Boolean).join(" · ")
    || "сравнить с одной моделью, переиграть проверку";
  $("pilotBody").innerHTML = `
    <h4>Ответ одной модели с поиском</h4>
    <p class="muted small">Контрольная группа: тот же вопрос — одной модели (${esc(p.solo_model || "")}) с поиском. Если её ответ не хуже решения совета,
      совет не окупается. Сравните и поставьте оценку обоим (решению совета — звёздами выше).</p>
    ${solo ? `${solo.truncated ? `<div class="alert warn small">Ответ упёрся в предел длины даже после продолжений и может быть оборван в конце.
        «Спросить заново» запросит его ещё раз.</div>` : ""}
      <div class="solo-answer md" id="soloAnswer">${md(solo.answer)}</div>
      ${(solo.citations || []).length ? `<details class="solo-src"><summary>Источники поиска (${solo.citations.length})</summary><ol>
        ${solo.citations.map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join("")}</ol></details>` : ""}
      <p class="muted small">${esc(solo.model)} · ${money(solo.cost)} · ${(solo.answer || "").split(/\s+/).filter(Boolean).length} слов${solo.parts > 1 ? ` · дописан в ${solo.parts} частях` : ""}${solo.mock ? " · демо" : ""}</p>
      <div class="solo-actions"><button type="button" class="btn small" id="soloCopy">Скопировать</button>
        <button type="button" class="btn small" id="soloSave">Скачать .md</button>
        <button type="button" class="btn small" id="soloAgain">Спросить заново</button></div>
      <div class="rate-row"><span class="muted small">Оценка ответа одной модели:</span><span class="stars" id="soloStars">${stars(p.solo_rating)}</span></div>`
      : `<button type="button" class="btn" id="soloBtn">Получить ответ одной модели</button> <span class="muted small">обычно $0,2–0,5</span>`}
    ${s.mode === "research" ? `<h4>Переиграть проверку утверждений</h4>
      <p class="muted small">Отбор утверждений и поиск заново — с текущими настройками исследования, на сохранённых вариантах и критике. Варианты не
        пишутся заново, поэтому это центы, а не новое заседание.</p>
      ${rep ? `<div class="table-wrap"><table class="table"><thead><tr><th></th><th class="num">Было</th><th class="num">Стало</th></tr></thead><tbody>
        ${row("Проверено утверждений", rep.before.checked, rep.after.checked)}${row("Подтверждено", rep.before.confirmed, rep.after.confirmed)}
        ${row("Не удалось проверить", rep.before.unclear, rep.after.unclear)}${row("Опровергнуто", rep.before.refuted, rep.after.refuted)}
        ${row("Непубличное — сразу в «неизвестно»", rep.before.deferred, rep.after.deferred)}${row("Источников", rep.before.sources, rep.after.sources)}
        ${row("Живых ссылок", rep.before.links_ok, rep.after.links_ok)}</tbody></table></div>
        <p class="muted small">Стоило ${money(rep.cost)} · настройки: ${esc(Object.entries(rep.settings || {}).map(([k, v]) => `${k}=${v}`).join(", "))}</p>` : ""}
      <button type="button" class="btn" id="replayBtn">${rep ? "Переиграть ещё раз" : "Переиграть проверку"}</button> <span class="muted small">обычно $0,2–0,8</span>` : ""}`;
  for (const id of ["soloBtn", "soloAgain"]) {
    const sb = document.getElementById(id);
    if (!sb) continue;
    const label = sb.textContent;
    sb.addEventListener("click", async () => {
      if (id === "soloAgain" && !confirm("Запросить ответ одной модели заново? Это снова оплачивается (обычно $0,2–0,5), прежний ответ заменится.")) return;
      sb.disabled = true; sb.textContent = "Модель отвечает — до нескольких минут…";
      try { await api(`/api/sessions/${encodeURIComponent(s.id)}/solo`, { method: "POST" }); loadPilot(s); }
      catch (e) { toast(e.message); sb.disabled = false; sb.textContent = label; }
    });
  }
  const sc = document.getElementById("soloCopy");
  if (sc) sc.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(solo.answer); toast("Ответ скопирован"); } catch { toast("Не удалось скопировать"); }
  });
  const ss = document.getElementById("soloSave");
  if (ss) ss.addEventListener("click", () => {
    const src = (solo.citations || []).length ? "\n\n## Источники поиска\n\n" + solo.citations.map((u, i) => `${i + 1}. ${u}`).join("\n") : "";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([solo.answer + src + "\n"], { type: "text/markdown;charset=utf-8" }));
    a.download = `${s.id}-odna-model.md`; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });
  const rb = document.getElementById("replayBtn");
  if (rb) rb.addEventListener("click", async () => {
    rb.disabled = true; rb.textContent = "Ищу заново…";
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/replay`, { method: "POST" }); loadPilot(s); }
    catch (e) { toast(e.message); rb.disabled = false; rb.textContent = "Переиграть проверку"; }
  });
  for (const b of $("pilotBody").querySelectorAll("[data-srate]")) b.addEventListener("click", async () => {
    const n = +b.dataset.srate === p.solo_rating ? 0 : +b.dataset.srate;
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/rating`, { method: "POST", body: { rating: n, kind: "solo" } }); p.solo_rating = n || null; renderPilot(s, p); }
    catch (e) { toast(e.message); }
  });
}

// куда ушли деньги: этапы, повторы и продолжения, доля входа из кэша
const costTable = (rows, total) => `<div class="table-wrap"><table class="table cost-table"><thead><tr><th>Этап</th>
  <th class="num">Обращений</th><th class="num" title="Ответ не по схеме, обрезан или дописывался продолжением">Повторы</th>
  <th class="num" title="Какая часть входных токенов прочитана из кэша провайдера (дешевле)">Из кэша</th><th class="num">Стоимость</th><th>Доля</th></tr></thead>
  <tbody>${rows.map((r) => `<tr><td>${esc(r.stage_name)}${r.member ? ` <span class="muted small">${esc(r.member)}</span>` : ""}</td>
    <td class="num">${r.requests}</td><td class="num" style="${r.extra ? "color:var(--warn)" : ""}">${r.extra}</td>
    <td class="num">${Math.round(r.cache_share * 100)}%</td><td class="num">${money(r.cost)}</td>
    <td><div class="win-cell"><div class="bar"><i style="width:${Math.round(r.share * 100)}%"></i></div>${Math.round(r.share * 100)}%</div></td></tr>`).join("")}</tbody></table></div>`;

async function loadCosts(sid) {
  let c;
  try { c = await api(`/api/sessions/${encodeURIComponent(sid)}/costs`); } catch { return; }
  if (!c.stages || !c.stages.length) return;
  $("costSec").hidden = false;
  $("costSum").textContent = `${money(c.total)} · из кэша ${Math.round(c.cache_share * 100)}% входа${c.extra_cost ? ` · на повторы ${money(c.extra_cost)}` : ""}`;
  const top = c.members.slice(0, 8);
  $("costBody").innerHTML = `<h4>По этапам</h4>${costTable(c.stages)}
    <h4>Самые дорогие участники на этапах</h4>${costTable(top)}
    <p class="muted small">Повторы — ответы не по схеме, обрезанные или дописанные продолжением. Из кэша — какая часть
      входа прочитана повторно и стоила дешевле.</p>`;
}

// оценка пользы под решением
async function loadRating(sid) {
  const row = $("rateRow");
  row.hidden = false;
  let cur = null;
  try { cur = (await api(`/api/sessions/${encodeURIComponent(sid)}/rating`)).rating; } catch { row.hidden = true; return; }
  renderStars(sid, cur ? cur.rating : 0);
}

function renderStars(sid, value) {
  $("rateStars").innerHTML = [1, 2, 3, 4, 5].map((n) => `<button type="button" class="star${n <= value ? " on" : ""}" role="radio"
    aria-checked="${n === value}" aria-label="${n} из 5" data-rate="${n}">${n <= value ? "★" : "☆"}</button>`).join("");
  $("rateNote").textContent = value ? "спасибо — учтено в «Статистике»" : "";
  for (const b of $("rateStars").querySelectorAll("[data-rate]")) b.addEventListener("click", async () => {
    const n = +b.dataset.rate === value ? 0 : +b.dataset.rate;  // повторное нажатие — убрать оценку
    try { await api(`/api/sessions/${encodeURIComponent(sid)}/rating`, { method: "POST", body: { rating: n } }); renderStars(sid, n); }
    catch (e) { toast(e.message); }
  });
}

/* ------------------------------------------------------------ уведомления и заголовок вкладки */

const BASE_TITLE = document.title;

function updateTitle() {
  const s = current;
  if (!s || view !== "session") { document.title = BASE_TITLE; return; }
  const t = shortTitle(s.question).slice(0, 40);
  if (s.error) document.title = `⚠️ Прервано — ${t}`;
  else if (s.done) document.title = `✅ Готово — ${t}`;
  else if (s.saving) document.title = "⏳ Отчёты… — Совет";
  else if (s.stage) {
    const names = Object.keys(s.work);
    const ready = names.filter((n) => s.work[n] !== "work"
      || (s.calls[n] && s.calls[n].stage === s.stage && ["done", "failed"].includes(s.calls[n].phase))).length;
    document.title = `⏳ ${STAGE_NAME[s.stage] || s.stage} ${ready}/${names.length} — Совет`;
  } else document.title = "⏳ Запуск — Совет";
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const tone = (freq, at) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = freq; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.35);
      o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + 0.4);
    };
    tone(880, 0); tone(1320, 0.18);
    setTimeout(() => ctx.close(), 1000);
  } catch { /* звук недоступен — не страшно */ }
}

function notifyFinished(s, ok) {
  if (!document.hidden) return;  // вкладка открыта — и так видно
  beep();
  const body = ok && s.verdict
    ? (s.verdict.acceptable === false ? "Ни один вариант не подходит" : `Решение: ${(s.proposals[s.verdict.winner] || {}).title || s.verdict.winner}`)
    : (s.error || "Заседание прервано");
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      const n = new Notification(ok ? "✅ Совет вынес решение" : "⚠️ Заседание прервано", { body: `${shortTitle(s.question)}\n${body}`, tag: s.id });
      n.onclick = () => { window.focus(); n.close(); };
    } catch { /* например, Safari без разрешения */ }
  }
}

/* ------------------------------------------------------------ полноэкранное окно */

let pendingProposal = null;
let modalReturnFocus = null;

function openModal({ eyebrow = "", title = "", sub = "", tabs = [], foot = "" }) {
  modalReturnFocus = document.activeElement;
  $("mEyebrow").textContent = eyebrow;
  $("mTitle").textContent = title;
  $("mSub").innerHTML = sub;
  $("mFoot").innerHTML = foot;
  const nav = $("mTabs");
  nav.hidden = tabs.length < 2;
  nav.innerHTML = tabs.map((t, i) => `<button type="button" role="tab" data-tab="${i}" aria-selected="${i === 0}">${esc(t.name)}</button>`).join("");
  const show = (i) => {
    $("mBody").innerHTML = tabs[i] ? tabs[i].html : "";
    $("mBody").scrollTop = 0;
    requestAnimationFrame(() => markAllScroll($("mBody")));
    for (const b of nav.querySelectorAll("button")) b.setAttribute("aria-selected", String(+b.dataset.tab === i));
  };
  nav.onclick = (e) => { const b = e.target.closest("[data-tab]"); if (b) show(+b.dataset.tab); };
  show(0);
  $("modal").hidden = false;
  document.body.classList.add("modal-open");
  $("mClose").focus();
}

let modalPushed = false;  // окно варианта открыто нашим шагом истории — тогда закрываем его «назад»
function closeModal(fromHistory = false) {
  if ($("modal").hidden) return;
  if (!fromHistory && current && /\/p\/[A-Z]$/.test(location.hash)) {
    if (modalPushed) { modalPushed = false; history.back(); return; }  // route() закроет окно
    history.replaceState(null, "", `#/s/${encodeURIComponent(current.id)}`);
  }
  modalPushed = false;
  $("modal").hidden = true;
  document.body.classList.remove("modal-open");
  if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
}

$("mClose").addEventListener("click", () => closeModal());
$("modal").addEventListener("keydown", (e) => trapFocus(e, $("modal")));
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("dialog").hidden) closeModal(); });

function proposalHtml(p, s, label) {
  const parts = [`<div class="section">${md(p.summary)}</div>`];
  if (p.reasoning) parts.push(`<h3>Обоснование</h3><div class="section">${md(p.reasoning)}</div>`);
  if (p.pros && p.pros.length) parts.push(`<h3>Плюсы</h3><ul>${p.pros.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.risks && p.risks.length) parts.push(`<h3>Риски</h3><ul>${p.risks.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.comparison) parts.push(`<h3>Сравнение с другими вариантами</h3><div class="section">${md(p.comparison)}</div>`);
  if (p.borrowed && p.borrowed.length) parts.push(`<h3>Взято у других</h3><ul>${p.borrowed.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.not_accepted && p.not_accepted.length) parts.push(`<h3>Что не принято из критики и почему</h3><ul>${p.not_accepted.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.changes) parts.push(`<div class="changes"><b>Что изменено после критики${POSITION_NAME[p.position] && p.comparison !== undefined ? ` (${POSITION_NAME[p.position]})` : ""}:</b> ${inline(p.changes)}</div>`);
  if (p.confidence != null && s.authors[label] != null) parts.push(`<p class="muted small">Уверенность автора: ${Math.round(p.confidence * 100)}%</p>`);
  return parts.join("");
}

// «VIOLATES R9: …» — служебная пометка рецензента: нарушено обязательное требование R9 из постановки
function flawHtml(x) {
  const m = String(x).match(/^\s*VIOLATES\s+(R\d+)\s*[:—–-]?\s*(.*)$/is);
  if (!m) return inline(x);
  const n = Number(m[1].slice(1)), req = ((current && current.requirements) || [])[n - 1];
  return `<span class="badge bad" ${req ? `title="${esc(req)}"` : ""}>нарушает ${esc(m[1].toUpperCase())}</span> ${inline(m[2])}`;
}

function reviewHtml(reviewer, r) {
  const scores = Object.entries(r.scores || {}).map(([k, v]) => `<span class="chip">${esc(k)}: <b>${v}</b></span>`).join("");
  return `<div class="review"><b>${esc(reviewer)}</b><div class="scores">${scores}</div>
    ${(r.strengths || []).length ? `<ul class="plus">${r.strengths.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>` : ""}
    ${(r.weaknesses || []).length ? `<ul class="minus">${r.weaknesses.map((x) => `<li>${flawHtml(x)}</li>`).join("")}</ul>` : ""}</div>`;
}

function openProposal(s, label) {
  const p = s.proposals[label];
  if (!p) return;
  const tabs = [{ name: s.revised[label] ? "Итоговая версия" : "Вариант", html: proposalHtml(p, s, label) }];
  const ini = s.initial[label];
  if (s.revised[label] && ini) tabs.push({ name: "До критики", html: `<h3>${esc(ini.title)}</h3>${proposalHtml(ini, s, label)}` });
  const reviews = [];
  for (const [rv, cs] of Object.entries(s.critiques)) for (const r of cs.reviews || []) if (r.label === label) reviews.push(reviewHtml(rv, r));
  if (reviews.length) tabs.push({ name: `Оценки (${reviews.length})`, html: reviews.join("") });
  const votes = Object.entries(s.ballots).filter(([, b]) => b.ranking.includes(label)).map(([voter, b]) =>
    `<li><b>${esc(voter)}</b>: место ${b.ranking.indexOf(label) + 1} из ${b.ranking.length} — <span class="muted">${esc(b.ranking.join(" › "))}</span></li>`);
  if (votes.length) tabs.push({ name: "Голоса", html: `<ul>${votes.join("")}</ul>` });
  const place = s.tally ? s.tally.findIndex((r) => r.label === label) + 1 : 0;
  const win = s.verdict && s.verdict.winner === label;
  const sub = [`Автор: <b>${esc(authorOf(s, label))}</b>`, place ? `место в голосовании: ${place}` : "", win ? "🏆 победитель" : "",
    s.revised[label] ? "доработан после критики" : ""].filter(Boolean).join(" · ");
  const foot = s.done ? `<a class="button secondary" href="${reportUrl(s.id, "html")}#proposal-${label}" target="_blank" rel="noopener">Открыть в отчёте ↗</a>` : "";
  const target = `#/s/${encodeURIComponent(s.id)}/p/${label}`;
  if (!sameHash(location.hash, target)) {
    if (/\/p\/[A-Z]$/.test(location.hash)) history.replaceState(null, "", target);  // другой вариант из открытого окна
    else { history.pushState(null, "", target); modalPushed = true; }
  }
  openModal({ eyebrow: `Вариант ${label}`, title: p.title, sub, tabs, foot });
}

function openReviewer(s, reviewer, label = null) {
  const cs = s.critiques[reviewer];
  if (!cs) return;
  const html = (cs.reviews || []).map((r) => {
    const p = s.proposals[r.label] || {};
    return reviewHtml(`${r.label}: ${p.title || ""}`, r).replace('class="review"', `class="review${r.label === label ? " hl" : ""}" id="rv-${esc(r.label)}"`);
  }).join("");
  openModal({ eyebrow: "Отзывы рецензента", title: reviewer, sub: `Оценил ${(cs.reviews || []).length} ${plural((cs.reviews || []).length, "вариант", "варианта", "вариантов")}`, tabs: [{ name: "Отзывы", html }] });
  if (label) requestAnimationFrame(() => { const el = document.getElementById(`rv-${label}`); if (el) el.scrollIntoView({ block: "start" }); });
}

$("matrix").addEventListener("click", (e) => {
  const th = e.target.closest("[data-reviewer]");
  if (th && current) openReviewer(current, th.dataset.reviewer, th.dataset.label || null);
});
$("matrix").addEventListener("keydown", (e) => {
  const th = e.target.closest("th[data-reviewer]");
  if (th && current && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openReviewer(current, th.dataset.reviewer); }
});

/* ------------------------------------------------------------ широкие таблицы: тень у края, где есть продолжение */

function markScroll(el) {
  const max = el.scrollWidth - el.clientWidth;
  el.classList.toggle("more-right", max > 1 && el.scrollLeft < max - 1);
  el.classList.toggle("more-left", max > 1 && el.scrollLeft > 1);
  // прокручиваемую таблицу можно листать и с клавиатуры (стрелками после Tab)
  if (max > 1) { el.tabIndex = 0; if (!el.getAttribute("aria-label")) el.setAttribute("aria-label", "Таблица, прокручивается вбок"); } else el.removeAttribute("tabindex");
}
function markAllScroll(root = document) { for (const el of root.querySelectorAll(".md-table, .table-wrap")) markScroll(el); }
document.addEventListener("scroll", (e) => { if (e.target.classList && (e.target.classList.contains("md-table") || e.target.classList.contains("table-wrap"))) markScroll(e.target); }, true);
window.addEventListener("resize", () => markAllScroll());

/* ------------------------------------------------------------ адреса: #/archive, #/s/<id>, #/s/<id>/p/<метка> */

function route() {
  const m = location.hash.match(/^#\/s\/([^/]+)(?:\/p\/([A-Z]))?$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (current && current.id === id && view === "session") {
      if (m[2] && current.done) openProposal(current, m[2]);
      else if (!m[2]) closeModal(true);  // «Назад» из окна варианта
      return;
    }
    openSession(id);
    if (m[2]) pendingProposal = m[2];
  } else if (location.hash === "#/archive") {
    if (view !== "archive") openArchive();
  } else if (location.hash === "#/stats") {
    if (view !== "stats") openStats().then(() => markAllScroll($("statsBody")));
  } else if (location.hash.startsWith("#/settings")) {
    if (view !== "settings" && window.openSettings) window.openSettings();
  } else if (!location.hash || location.hash === "#" || location.hash === "#/") {
    closeModal(true);
    if (view !== "empty") { goCompose(); window.scrollTo({ top: 0 }); }
  }
}

/* ------------------------------------------------------------ старт */

(async () => {
  try {
    await initForm();
  } catch (e) {
    showFormError(`Не удалось загрузить конфигурацию: ${e.message}`);
  }
  await loadHistory();
  loadPaused();
  if (!location.hash) showView("empty");
  route();
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
})();


/* ------------------------------------------------------------ «Подобрать состав» */

const BASIS_TEXT = { catalog: "каталог", history: "по вашим прошлым заседаниям", reviews: "обзоры", knowledge: "знания советника" };
const BASIS_HINT = {
  catalog: "факты каталога OpenRouter: контекст, предел ответа, рассуждения, цена",
  history: "ваши заседания: победы вариантов, оценки критиков, сбои",
  reviews: "обзоры, бенчмарки и известные проблемы — найдены поиском в интернете",
  knowledge: "общие знания модели-советника — могут устареть",
};
const ROLE_BADGE = { chair: ["ok", "председатель"], devil: ["warn", "адвокат дьявола"], member: ["plain", "участник"] };
const SIZE_TEXT = { small: "небольшая задача", medium: "средняя задача", large: "большая задача" };
let lastAdvice = null;

async function runAdvice(refresh) {
  const p = payload();
  if (p.question.length < 3) { toast("Сначала напишите вопрос"); $("question").focus(); return; }
  const box = $("adviceBox");
  box.hidden = false;
  box.innerHTML = `<p class="muted"><span class="spinner" aria-hidden="true"></span> ${p.mock ? "Подбираю по правилам (демо)…"
    : "Собираю обзоры моделей и разбираю вопрос — обычно 20–60 секунд…"}</p>`;
  $("adviseBtn").disabled = true;
  try {
    const atts = uploads.filter((u) => u.status === "ready").map((u) => ({ name: u.meta.name, kind: u.meta.kind, chars: u.meta.chars || 0 }));
    lastAdvice = await api("/api/advise", { method: "POST", body: {
      question: p.question, mode, members: p.members, chair: p.chair, devil_advocate: p.devil_advocate,
      budget: p.budget, attachments: atts, followup: !!p.parent, mock: p.mock, refresh } });
    renderAdvice(lastAdvice);
  } catch (e) {
    box.innerHTML = `<p class="bad-text">Не удалось подобрать состав: ${esc(e.message)}</p>`;
  } finally { $("adviseBtn").disabled = false; }
}

function renderAdvice(r) {
  const a = r.advice, t = a.task || {}, f = r.facts || {}, ds = r.dossiers || {};
  const k = (n) => (n == null ? "" : n >= 1000 ? `${Math.round(n / 1000)} тыс.` : String(n));
  const basis = (b) => (b || []).map((x) => `<span class="basis b-${esc(x)}" title="${esc(BASIS_HINT[x] || "")}">${esc(BASIS_TEXT[x] || x)}</span>`).join("");
  const src = r.source === "advisor" ? `советник: ${esc((r.advisor_model || "").split("/").pop())}`
    : "подобрано по настройкам и возможностям моделей — без модели-советника";
  const cost = [src, r.cost ? `$${r.cost.toFixed(3)}` : "", r.dossiers_built ? `обзоров собрано ${r.dossiers_built}` : "",
    r.dossiers_cached ? `из памяти ${r.dossiers_cached}` : ""].filter(Boolean).join(" · ");
  const newMode = a.mode && a.mode !== mode;
  const picks = (a.picks || []).map((p, i) => {
    const [cls, role] = ROLE_BADGE[p.role] || ROLE_BADGE.member;
    const x = f[p.model] || {}, d = ds[p.model];
    const [prov, ...rest] = p.model.split("/");
    const meta = [x.reasoning ? "рассуждает" : "без рассуждений", x.context ? `контекст ${k(x.context)}` : "",
      x.max_out ? `ответ до ${k(x.max_out)}` : "", x.price_out ? `$${x.price_in} / $${x.price_out} за 1M` : ""].filter(Boolean).join(" · ");
    const issues = d ? (d.weaknesses || []).concat(d.known_issues || []) : [];
    const rev = d && ((d.strengths || []).length || issues.length) ? `<details class="adv-rev"><summary>Что пишут о модели</summary>
      ${(d.strengths || []).length ? `<p><b>Сильные стороны.</b> ${d.strengths.map(esc).join("; ")}</p>` : ""}
      ${issues.length ? `<p><b>Слабые стороны.</b> ${issues.map(esc).join("; ")}</p>` : ""}
      ${(d.sources || []).length ? `<p class="small">${d.sources.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\/(www\.)?/, "").split("/")[0])}</a>`).join(" · ")}</p>` : '<p class="muted small">ссылок от поиска нет — проверьте сами</p>'}</details>` : "";
    return `<li class="adv-pick r-${esc(p.role)}">
      <div class="adv-role"><label class="adv-take"><input type="checkbox" data-pi="${i}" checked> <span class="role-tag ${cls}">${role}</span></label>
        ${p.web_search ? `<label class="adv-take small"><input type="checkbox" data-pw="${i}" checked> 🌐 с поиском</label>` : ""}</div>
      <div class="adv-main">
        <div class="adv-name"><b>${esc(rest.join("/") || p.model)}</b> <span class="muted small">${esc(prov)}</span>
          <button type="button" class="link small adv-card" data-card="${esc(p.model)}">О модели</button></div>
        <div class="adv-meta">${esc(meta)}</div>
        <p class="adv-why">${esc(p.why)} ${basis(p.basis)}${p.confidence ? ` <span class="basis" title="насколько советник уверен">уверенность: ${esc({ low: "низкая", medium: "средняя", high: "высокая" }[p.confidence] || p.confidence)}</span>` : ""}</p>${rev}
      </div></li>`;
  }).join("");
  const drops = selectedMembers().filter((n) => !(r.plan.members || []).includes(n));
  const avoid = (a.avoid || []).length ? `<h4>Хуже подходят для этого вопроса</h4><ul class="adv-avoid">${a.avoid.map((x) =>
    `<li><b>${esc(x.model.split("/").pop())}</b> — ${esc(x.why)} ${basis(x.basis)}</li>`).join("")}</ul>` : "";
  $("adviceBox").innerHTML = `
    <div class="adv-top"><h3>✨ Рекомендация</h3><span class="muted small">${cost}</span></div>
    <dl class="adv-sum">
      <dt>Задача</dt><dd><b>${esc(t.kind || "—")}</b>${t.size ? ` · ${esc(SIZE_TEXT[t.size] || t.size)}` : ""}${(t.needs || []).map((n) => ` <span class="need">${esc(n)}</span>`).join("")}
        ${t.summary ? `<div class="muted small">${esc(t.summary)}</div>` : ""}</dd>
      <dt>Режим</dt><dd>${newMode ? `<label class="adv-take"><input type="checkbox" id="advMode" checked> <b>${esc(MODE_NAME[a.mode] || a.mode)}</b></label> <span class="muted small">(сейчас выбран «${esc(MODE_NAME[mode] || mode)}»)</span>`
        : `<b>${esc(MODE_NAME[a.mode || mode] || mode)}</b>`}
        ${a.mode_why ? `<div class="muted small">${esc(a.mode_why)}</div>` : ""}</dd>
    </dl>
    ${Object.keys(a.research_settings || {}).length ? `<h4>Настройки исследования для этого вопроса</h4>
      <ul class="adv-rs">${Object.entries(a.research_settings).map(([k, v]) => `<li><label class="rs-row"><input type="checkbox" data-rs="${esc(k)}" checked>
        <span class="rs-body"><span class="rs-name">${esc(RS_NAME[k] || k)}:</span> <b>${esc(rsVal(v))}</b>
          <span class="muted small">(сейчас ${esc(rsVal((config.research_all || {})[k]))}${(a.research_cost || {})[k] != null ? ` · ${a.research_cost[k] >= 0 ? "+" : "−"}$${Math.abs(a.research_cost[k]).toFixed(2)} к цене${r.research_cost_by === "estimate" ? " (расчёт по каталогу)" : " (оценка советника)"}` : ""})</span>
          ${(a.research_why || {})[k] ? `<span class="rs-why">${esc(a.research_why[k])}</span>` : ""}</span></label></li>`).join("")}</ul>` : ""}
    ${a.baseline ? `<p class="muted small adv-base"><b>Исходная точка:</b> ${esc(a.baseline)}</p>` : ""}
    <h4>Состав</h4><ol class="adv-picks">${picks}</ol>
    ${drops.length ? `<label class="adv-take adv-drop"><input type="checkbox" id="advDrop" checked> Убрать из текущего состава: <b>${drops.map(esc).join(", ")}</b>
      <span class="muted small">— их нет в рекомендации</span></label>` : ""}
    <p class="muted small">Снимите галочки с того, что брать не нужно: неотмеченное останется как сейчас.</p>
    ${avoid}
    ${(a.caveats || []).length ? `<ul class="adv-caveats">${a.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    <div class="adv-actions"><button type="button" class="btn primary" id="adviceApply">Применить выбранное</button>
      <button type="button" class="btn" id="adviceClose">Закрыть</button>
      ${r.source === "advisor" ? '<button type="button" class="link small" id="adviceRefresh">Обновить обзоры моделей</button>' : ""}</div>
    <p class="muted small adv-foot">Настройки программы не меняются; постоянный состав — «Настройки → Модели».</p>`;
  $("adviceApply").addEventListener("click", () => applyAdvice(r));
  // поиск у модели нельзя взять без самой модели
  $("adviceBox").querySelectorAll("[data-pi]").forEach((cb) => cb.addEventListener("change", () => {
    const w = $("adviceBox").querySelector(`[data-pw="${cb.dataset.pi}"]`);
    if (w) { w.disabled = !cb.checked; if (!cb.checked) w.checked = false; }
  }));
  $("adviceClose").addEventListener("click", () => { $("adviceBox").hidden = true; });
  const rf = document.getElementById("adviceRefresh");
  if (rf) rf.addEventListener("click", () => runAdvice(true));
  $("adviceBox").querySelectorAll("[data-card]").forEach((b) => b.addEventListener("click", () => openModelCard(b.dataset.card)));
}

// снимок формы до применения рекомендаций — чтобы «Отменить» вернул как было
function formSnapshot() {
  return { mode, members: selectedMembers(), chair: $("chair").value, devil: $("devil").value,
    lineup: { models: { ...lineup.models }, extra: lineup.extra.map((x) => ({ ...x })), web: new Set(lineup.web) },
    researchTuned: { ...researchTuned }, words: $("researchWords").value, shorten: $("researchShorten").checked,
    noLimit: $("researchNoLimit").checked };
}
function restoreSnapshot(snap) {
  lineup = { models: { ...snap.lineup.models }, extra: snap.lineup.extra.map((x) => ({ ...x })), web: new Set(snap.lineup.web) };
  if (snap.mode !== mode) setMode(snap.mode);
  renderMembers(new Set(snap.members));
  refreshRoleSelects();
  $("chair").value = snap.chair; $("devil").value = snap.devil;
  researchTuned = { ...snap.researchTuned };
  $("researchWords").value = snap.words; $("researchShorten").checked = snap.shorten; $("researchNoLimit").checked = !!snap.noLimit;
  updateShortenHint(); renderResearchTuned(); updateChairHint(); updateResetBtn(); formChanged();
}

function applyAdvice(r) {
  const before = formSnapshot();
  const plan = r.plan, a = r.advice, box = $("adviceBox");
  const on = (sel) => { const el = box.querySelector(sel); return !!(el && el.checked); };
  const take = (a.picks || []).map((_, i) => on(`[data-pi="${i}"]`));
  const modeOn = a.mode && a.mode !== mode ? on("#advMode") : false;
  const rsOn = Object.keys(a.research_settings || {}).filter((k) => on(`[data-rs="${CSS.escape(k)}"]`));
  if (!take.some(Boolean) && !modeOn && !rsOn.length && !on("#advDrop")) { toast("Ничего не отмечено"); return; }
  const base = Object.fromEntries(config.members.map((m) => [m.name, m.model]));
  const sel = new Set(selectedMembers());
  const chairBefore = $("chair").value, devilBefore = $("devil").value;
  if (on("#advDrop")) for (const n of [...sel]) if (!plan.members.includes(n)) sel.delete(n);
  (a.picks || []).forEach((p, i) => {
    if (!take[i]) return;
    const slot = plan.members[i];
    sel.add(slot);
    const extra = (plan.extra_members || []).find((x) => x.name === slot);
    if (extra && !lineup.extra.some((x) => x.name === slot)) lineup.extra.push({ ...extra });
    if (plan.models[slot] && base[slot]) {
      if (plan.models[slot] === base[slot]) delete lineup.models[slot]; else lineup.models[slot] = plan.models[slot];
    }
    if (p.web_search && on(`[data-pw="${i}"]`)) lineup.web.add(slot); else lineup.web.delete(slot);
  });
  if (modeOn && mode !== "evaluate") setMode(a.mode);
  renderMembers(sel);
  refreshRoleSelects();
  const ci = (a.picks || []).findIndex((p) => p.role === "chair");
  const di = (a.picks || []).findIndex((p) => p.role === "devil");
  $("chair").value = ci >= 0 && take[ci] ? plan.chair : sel.has(chairBefore) ? chairBefore : $("chair").value;
  $("devil").value = di >= 0 && take[di] ? plan.devil_advocate : sel.has(devilBefore) ? devilBefore : "";
  if (mode === "research") {
    const rs = Object.fromEntries(rsOn.map((k) => [k, a.research_settings[k]]));
    if (rs.max_words) { $("researchWords").value = rs.max_words; $("researchNoLimit").checked = false; updateShortenHint(); }
    if (rs.shorten_long !== undefined) $("researchShorten").checked = !!rs.shorten_long;
    researchTuned = { ...researchTuned, ...rs };
    renderResearchTuned();
  }
  updateChairHint();
  updateResetBtn();
  $("params").open = true;
  formChanged();
  box.hidden = true;
  const n = take.filter(Boolean).length + (modeOn ? 1 : 0) + rsOn.length + (on("#advDrop") ? 1 : 0);
  toastAction(`Применено ${n} ${plural(n, "изменение", "изменения", "изменений")} — только для этого вопроса`, "Отменить", () => {
    restoreSnapshot(before); box.hidden = false; toast("Вернули как было");
  });
}

// на что опирается пункт «известно»: проверенная формулировка реестра или цитата из документа пользователя
const SEEN_TEXT = { page: "страница прочитана", snippet: "только фрагмент выдачи", search_answer: "только ответ поиска" };
const LINK_TEXT = { ok: "ссылка открывается", dead: "ссылка не открывается", blocked: "сайт не пускает проверку", error: "не открылась при проверке" };
function knownBasis(k, s) {
  const same = (a, b) => (a || "").trim().toLowerCase() === (b || "").trim().toLowerCase();
  const v = (k.verified || []).filter((t) => !same(t, k.statement));
  const out = [];
  // индикатор доверия: откуда, когда, что реально видела модель, открывается ли ссылка, найдена ли цитата
  const led = (s && s.ledger) || {};
  const evs = Object.fromEntries((led.evidence || []).map((e) => [e.id, e]));
  const claims = Object.fromEntries((led.claims || []).map((c) => [c.id, c]));
  const quote = (k.claim_ids || []).some((c) => claims[c] && claims[c].quote_found);
  const trust = (k.evidence_ids || []).filter((i) => evs[i]).map((i) => {
    const e = evs[i];
    let host = ""; try { host = new URL(e.url).hostname.replace(/^www\./, ""); } catch { host = e.url || ""; }
    return [`${i}: ${host}`, e.date ? `от ${e.date}` : "", SEEN_TEXT[e.material_seen] || "", LINK_TEXT[e.link] || ""].filter(Boolean).join(", ");
  });
  const sem = s && s.semantic && s.semantic.items && s.semantic.items[k.item_id];
  if (sem && sem.status !== "supported") out.push(`<div class="small"><span class="badge warn" title="Теневая сверка смысла одной моделью — только для сведения, статус не менялся">сверка смысла: ${sem.status === "contradicted" ? "противоречит опоре" : "следует не полностью"}</span> <span class="muted">${esc([sem.reason, (sem.unsupported_parts || []).join("; ")].filter(Boolean).join(" — "))}</span></div>`);
  if (trust.length) out.push(`<div class="trust small">${trust.map(esc).join(" · ")}${quote ? ' · <span class="ok-text">цитата найдена на странице</span>' : ""}</div>`);
  if (v.length) out.push(`<div class="muted small">проверено: «${v.map(esc).join("», «")}»</div>`);
  if ((k.evidence_ids || []).includes("DOC")) out.push(`<div class="muted small">по вашему документу${k.doc_ref ? ` (${esc(k.doc_ref)})` : ""}${k.doc_quote ? `: «${esc(k.doc_quote)}»` : ""} — не независимая проверка</div>`);
  return out.join("");
}

// основания вывода — отдельно от согласия моделей: что подтверждено источниками, что нет
function basisBadge(s) {
  const led = s.ledger && s.ledger.claims;
  if (s.mode === "research") {
    if (!led || !led.length) return '<span class="badge plain" title="Утверждения поиском не проверялись">Основания: источники не проверялись</span>';
    const own = led.filter((c) => !c.duplicate_of);
    const ok = own.filter((c) => c.status === "confirmed").length;
    const kind = ok === 0 ? "bad" : ok * 2 < own.length ? "warn" : "ok";
    return `<span class="badge ${kind}" title="Сколько проверенных утверждений подтвердили найденные источники; остальное — в «спорно» и «неизвестно»">Основания: подтверждено ${ok} из ${own.length}</span>`;
  }
  const facts = s.facts ? Object.values(s.facts).flatMap((r) => r.claims || []) : [];
  if (!facts.length) return '<span class="badge plain" title="Факты поиском не проверялись — вывод опирается на рассуждения моделей">Факты не проверялись</span>';
  const ok = facts.filter((c) => c.status === "confirmed").length;
  return `<span class="badge ${ok * 2 < facts.length ? "warn" : "ok"}">Факты: подтверждено ${ok} из ${facts.length}</span>`;
}

// уведомление с действием («Отменить»)
function toastAction(text, label, fn) {
  const t = document.createElement("div");
  t.className = "toast toast-act";
  t.setAttribute("role", "status");
  t.innerHTML = `<span>${esc(text)}</span> <button type="button" class="link">${esc(label)}</button>`;
  document.body.appendChild(t);
  const close = () => t.remove();
  t.querySelector("button").addEventListener("click", () => { close(); fn(); });
  setTimeout(close, 9000);
}

// «Как работает совет» — коротко, по запросу
$("howBtn").addEventListener("click", () => {
  const open = $("howBox").hidden;
  $("howBox").hidden = !open;
  $("howBtn").setAttribute("aria-expanded", String(open));
});

/* ------------------------------------------------------------ пять решений владельца: глубина, свежие данные, объём */
// пресеты исследования — стартовые значения из третьей проверки совета (гипотезы для пилота, а не замеры)
const DEPTH = {
  fast: { label: "Быстро и дёшево", budget: 4, words: 800,
    rs: { max_claims: 5, arbitration_claims: 10, missions: 1, recheck_unclear: 0, post_revision_claims: 2, section_vote: false, digest_table_rows: 12 } },
  standard: { label: "Стандарт", budget: 8, words: 1400,
    rs: { max_claims: 8, arbitration_claims: 20, missions: 2, recheck_unclear: 3, post_revision_claims: 4, section_vote: true, digest_table_rows: 12 } },
  deep: { label: "Тщательно", budget: 12, words: 2000,
    rs: { max_claims: 12, arbitration_claims: 30, missions: 2, recheck_unclear: 5, post_revision_claims: 4, section_vote: true, digest_table_rows: 24 } },
};
$("depth").addEventListener("change", () => {
  const d = DEPTH[$("depth").value];
  if (!d) { researchTuned = {}; $("depthHint").textContent = ""; renderResearchTuned(); formChanged(); return; }
  researchTuned = { ...d.rs };
  $("researchWords").value = d.words; $("researchNoLimit").checked = false; updateShortenHint();
  $("budget").value = d.budget;
  $("depthHint").textContent = `вариант до ${d.words} слов · утверждений у автора до ${d.rs.max_claims} · проверка ${d.rs.arbitration_claims} утв.`
    + ` · второй поиск ${d.rs.recheck_unclear} · бюджет $${d.budget}. Правила «известно» не ослабляются.`;
  renderResearchTuned(); formChanged();
});
// свежие данные: сколько авторов ищут в интернете (кто именно — в «Составе и параметрах»)
function freshAuthors() {
  const chair = $("chair").value;
  return selectedMembers().filter((n) => n !== chair || $("chairProposes").checked);
}
function syncFresh() {
  const web = [...lineup.web].filter((n) => selectedMembers().includes(n));
  const authors = freshAuthors();
  const n = web.length;
  const standard = n <= 2 && web.every((x, i) => x === authors[i]);
  $("fresh").value = standard ? String(n) : "custom";
  $("fresh").querySelector('[value="custom"]').hidden = standard;
}
$("fresh").addEventListener("change", () => {
  const n = Number($("fresh").value);
  if (Number.isNaN(n)) return;
  lineup.web = new Set(freshAuthors().slice(0, n));
  renderWebMembers(); formChanged();
});
$("answerSize").addEventListener("change", formChanged);

// охват по объектам (компании, продукты): сколько утверждений о каждом проверено и подтверждено — для каталогов
function entityCoverage(s) {
  const claims = ((s.ledger && s.ledger.claims) || []).filter((c) => c.entity && !c.duplicate_of && !c.derived_from);
  const by = {};
  for (const c of claims) {
    const k = c.entity.trim();
    const r = by[k] || (by[k] = { n: 0, ok: 0 });
    r.n += 1; if (c.status === "confirmed") r.ok += 1;
  }
  for (const c of ((s.ledger && s.ledger.claims) || [])) if (c.derived_from && c.entity && by[c.entity.trim()] && c.status === "confirmed") by[c.entity.trim()].ok += 1;
  const rows = Object.entries(by).sort((a, b) => b[1].n - a[1].n);
  if (rows.length < 3) return "";
  return `<h4>Охват по объектам</h4><p class="muted small">Сколько утверждений о каждом объекте проверено поиском и сколько подтверждено. Объектов, которых здесь нет, поиск не проверял — данные о них из памяти моделей.</p>
    <div class="table-wrap"><table class="table"><thead><tr><th>Объект</th><th class="num">Проверено</th><th class="num">Подтверждено</th></tr></thead><tbody>
    ${rows.map(([k, r]) => `<tr><td>${esc(k)}</td><td class="num">${r.n}</td><td class="num">${r.ok}</td></tr>`).join("")}</tbody></table></div>`;
}

// длина варианта исследования не ограничена (research_words = 0)
function noLimit() { return !!($("researchNoLimit") && $("researchNoLimit").checked); }
