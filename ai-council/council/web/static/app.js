"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x) => (x == null ? "—" : `$${Number(x).toFixed(4)}`);

const STAGES = [
  ["framing", "Постановка"],
  ["proposal", "Предложения"],
  ["critique", "Критика"],
  ["revision", "Доработка"],
  ["vote", "Голосование"],
  ["verdict", "Вердикт"],
];
const STAGE_NAME = Object.fromEntries(STAGES);
const MODE_HINT = {
  debate: "Предложения → критика → доработка → голосование → вердикт. Для важных решений.",
  quick: "Предложения → голосование → вердикт. Быстро и дёшево.",
  evaluate: "Вы даёте готовые варианты, совет их критикует и выбирает лучший.",
};

/* ------------------------------------------------------------ markdown (минимальный, безопасный) */

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
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
    if ((m = line.match(/^#{1,6}\s+(.*)$/))) { out.push(`<h4>${inline(m[1])}</h4>`); i++; continue; }
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
  for (const b of $("modeSeg").querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.mode === m));
  $("roundsField").hidden = m !== "debate";
  $("optionsField").hidden = m !== "evaluate";
  $("modeHint").textContent = MODE_HINT[m];
  $("estimate").hidden = true;
}

function selectedMembers() {
  return [...$("members").querySelectorAll("input:checked")].map((i) => i.value);
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
  fill($("chair"), false, config.chair);
  fill($("devil"), true, config.devil_advocate);
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
    chair: $("chair").value || null,
    devil_advocate: $("devil").value,
    budget: Number($("budget").value) || null,
    mock: $("mock").checked,
  };
}

function showFormError(msg) {
  $("formError").hidden = !msg;
  $("formError").textContent = msg || "";
}

function renderEstimate(e) {
  const rows = e.members.map((m) =>
    `<tr><td>${esc(m.name)}</td><td>${m.calls} выз.</td><td>${m.cost == null ? "?" : money(m.cost)}</td></tr>`).join("");
  let html = "";
  if (e.low != null) {
    html += `<div class="big">≈ $${e.low.toFixed(3)} – $${e.high.toFixed(3)}</div>`;
    html += `<div class="muted">${e.calls} вызовов · лимит $${e.budget.toFixed(2)}</div>`;
    if (e.high > e.budget) html += `<div class="warnbox">Верхняя оценка выше лимита — заседание может остановиться по бюджету.</div>`;
  } else {
    html += `<div class="big">${e.calls} вызовов</div><div class="muted">${$("mock").checked ? "Демо-режим: бесплатно." : "Цены неизвестны."}</div>`;
  }
  if (e.warning) html += `<div class="warnbox">${esc(e.warning)}</div>`;
  const unknown = Object.entries(e.unknown || {});
  if (unknown.length) {
    html += `<div class="error">Этих моделей нет на OpenRouter — исправьте config.yaml или снимите участника:\n${
      unknown.map(([m, s]) => `• ${m}${s.length ? ` (возможно: ${s.join(", ")})` : ""}`).join("\n")}</div>`;
  }
  html += `<table>${rows}</table>`;
  $("estimate").innerHTML = html;
  $("estimate").hidden = false;
  return unknown.length === 0;
}

async function doEstimate() {
  showFormError("");
  const p = payload();
  if (p.question.length < 3) { showFormError("Введите вопрос"); return null; }
  try {
    return await api("/api/estimate", { method: "POST", body: p });
  } catch (e) { showFormError(e.message); return null; }
}

async function initForm() {
  config = await api("/api/config");
  $("members").innerHTML = config.members.map((m) => `
    <label class="member"><input type="checkbox" value="${esc(m.name)}" ${m.enabled ? "checked" : ""}>
      <b>${esc(m.name)}</b><code title="${esc(m.model)}">${esc(m.model)}</code></label>`).join("");
  $("members").addEventListener("change", () => { refreshRoleSelects(); $("estimate").hidden = true; });
  refreshRoleSelects();
  $("budget").value = config.budget;
  $("rounds").value = Math.max(1, config.rounds);
  $("mock").checked = !config.has_key;
  const ks = $("keyStatus");
  ks.textContent = config.has_key ? "Ключ OpenRouter подключён" : "Нет ключа — только демо";
  ks.className = `pill ${config.has_key ? "ok" : "warn"}`;
  setMode(config.mode === "evaluate" ? "debate" : config.mode);
}

$("modeSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); });
for (const id of ["question", "options", "criteria", "rounds", "mock", "budget"]) $(id).addEventListener("input", () => { $("estimate").hidden = true; });

$("estimateBtn").addEventListener("click", async () => {
  const e = await doEstimate();
  if (e) renderEstimate(e);
});

$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showFormError("");
  const p = payload();
  if (p.question.length < 3) return showFormError("Введите вопрос");
  if (p.members.length < 2) return showFormError("Выберите хотя бы двух участников");
  if (mode === "evaluate" && p.options.length < 2) return showFormError("Для оценки нужно минимум 2 варианта");
  const btn = $("startBtn");
  btn.disabled = true;
  try {
    if (!p.mock) {
      const e = await doEstimate();
      if (!e) return;
      if (!renderEstimate(e)) return;
      if (e.low != null && !confirm(`Оценка стоимости: $${e.low.toFixed(3)} – $${e.high.toFixed(3)}. Начать заседание?`)) return;
    }
    const { id } = await api("/api/sessions", { method: "POST", body: p });
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
  $("empty").hidden = name !== "empty";
  $("session").hidden = name !== "session";
  $("archive").hidden = name !== "archive";
  document.body.classList.toggle("has-session", name !== "empty");
  $("archiveBtn").classList.toggle("secondary", name === "archive");
}

$("newBtn").addEventListener("click", () => {
  closeStream();
  current = null;
  history.replaceState(null, "", location.pathname);
  showView("empty");
  markActiveHistory();
  $("question").focus();
});

function reportUrl(id, fmt, download) {
  return `/api/sessions/${encodeURIComponent(id)}/report?format=${fmt}${download ? "&download=1" : ""}`;
}
function downloadLinks(id, cls = "button secondary") {
  return formats.map((f) => f === "pdf"
    ? `<a class="${cls}" href="${reportUrl(id, "pdf")}" target="_blank" rel="noopener">PDF</a>`
    : `<a class="${cls}" href="${reportUrl(id, f, true)}">${FORMAT_NAME[f]}</a>`).join("")
    + `<a class="${cls}" href="${reportUrl(id, "html")}" target="_blank" rel="noopener" title="Полный отчёт в новой вкладке">Открыть отчёт ↗</a>`;
}

/* ------------------------------------------------------------ история */

const HISTORY_LIMIT = 12;
async function loadHistory() {
  let data;
  try { data = await api("/api/sessions"); } catch { return; }
  formats = data.formats || formats;
  const items = data.items || [];
  const ul = $("history");
  $("allArchiveBtn").hidden = !items.length;
  $("allArchiveBtn").textContent = `Весь архив (${data.total}) →`;
  if (!items.length) { ul.innerHTML = '<li class="muted">Пока пусто</li>'; return; }
  const statusText = { running: "идёт…", error: "ошибка", cancelled: "остановлено" };
  ul.innerHTML = items.slice(0, HISTORY_LIMIT).map((it) => {
    const date = it.date || it.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/, "$3.$2 $4:$5");
    const meta = it.status === "done"
      ? `${esc(date)} · ${esc(it.winner_title || "")}${it.cost != null ? ` · ${money(it.cost)}` : ""}`
      : `${esc(date)} · ${statusText[it.status] || it.status}`;
    return `<li class="item" data-id="${esc(it.id)}"><div class="q"><div title="${esc(it.question)}">${esc(shortTitle(it.question))}</div><div class="meta">${meta}</div></div>
      ${it.status === "running" ? "" : `<button class="del" title="Удалить" data-del="${esc(it.id)}" aria-label="Удалить">×</button>`}</li>`;
  }).join("");
  markActiveHistory();
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
  $("archiveCount").textContent = q || mode ? `Найдено: ${items.length}` : `Всего заседаний: ${items.length}`;
  if (!items.length) {
    $("archiveList").innerHTML = `<div class="arc-empty">${q || mode ? "Ничего не найдено" : "Архив пуст — проведите первое заседание"}</div>`;
    return;
  }
  $("archiveList").innerHTML = items.map((it) => {
    const done = it.status === "done";
    const meta = [it.date, it.mode_name, it.members && it.members.length ? `участников: ${it.members.length}` : "",
      it.cost != null ? money(it.cost) : "", done ? "" : ({ running: "идёт…", error: "ошибка", cancelled: "остановлено" }[it.status] || "")]
      .filter(Boolean).map(esc).join(" · ");
    const actions = done
      ? `${downloadLinks(it.id, "button ghost")}<button type="button" class="ghost" data-arc-del="${esc(it.id)}" title="Удалить">Удалить</button>`
      : "";
    return `<div class="arc-item">
      <div class="arc-q" data-open="${esc(it.id)}" tabindex="0" role="link" title="${esc(it.question)}">${esc(shortTitle(it.question))}</div>
      <div class="arc-actions">${actions}</div>
      ${done && it.winner ? `<div class="arc-win">🏆 <b>${esc(it.winner)}</b>: ${esc(it.winner_title)} <span class="muted">(${esc(it.winner_author)})</span></div>` : ""}
      <div class="arc-meta">${meta}</div>
    </div>`;
  }).join("");
}

function openArchive() {
  closeStream();
  current = null;
  history.replaceState(null, "", "#/archive");
  showView("archive");
  markActiveHistory();
  loadArchive();
}

$("archiveBtn").addEventListener("click", openArchive);
$("allArchiveBtn").addEventListener("click", openArchive);
$("archiveSearch").addEventListener("input", () => { clearTimeout(archiveTimer); archiveTimer = setTimeout(loadArchive, 250); });
$("archiveMode").addEventListener("change", loadArchive);
$("archiveList").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-arc-del]");
  if (del) {
    if (!confirm("Удалить это заседание и все его отчёты?")) return;
    try { await api(`/api/sessions/${encodeURIComponent(del.dataset.arcDel)}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
    loadArchive(); loadHistory();
    return;
  }
  const open = e.target.closest("[data-open]");
  if (open) openSession(open.dataset.open);
});
$("archiveList").addEventListener("keydown", (e) => {
  const open = e.target.closest("[data-open]");
  if (open && e.key === "Enter") openSession(open.dataset.open);
});

$("history").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("Удалить это заседание из истории?")) return;
    try { await api(`/api/sessions/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
    if (current && current.id === del.dataset.del) $("newBtn").click();
    loadHistory();
    return;
  }
  const li = e.target.closest("li.item");
  if (li) openSession(li.dataset.id);
});

function markActiveHistory() {
  for (const li of $("history").querySelectorAll("li.item")) li.classList.toggle("active", !!current && li.dataset.id === current.id);
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
    startedAt: Date.now(), initial: {},
    calls: {}, log: [], stageStart: null, lastMsg: Date.now(), wrote: {},
  };
}

const PHASE_ACTIVE = new Set(["queued", "sent", "thinking", "writing", "checking", "retry"]);
function logAdd(s, text, kind = "") {
  s.log.push({ t: new Date(), text, kind });
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
  switch (ev.type) {
    case "ping":
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
      c.since = Date.now() - (ev.elapsed || 0) * 1000;  // начало обращения по часам браузера
      c.updated = Date.now();
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
      Object.assign(s, { question: ev.question, mode: ev.mode, rounds: ev.rounds, members: ev.members || [],
        chair: ev.chair || null, mock: !!ev.mock });
      break;
    case "stage": {
      s.stageStart = Date.now();
      logAdd(s, `▶ ${ev.title}`, "k-stage");
      if (s.stage && s.stage !== ev.stage) s.doneStages.add(s.stage);
      s.stage = ev.stage;
      s.stageTitle[ev.stage] = ev.title;
      const who = ["framing", "verdict"].includes(ev.stage) ? [s.chair].filter(Boolean) : s.members;
      s.work = Object.fromEntries(who.map((n) => [n, "work"]));
      break;
    }
    case "framing":
      s.refined = ev.refined_question; s.criteria = ev.criteria;
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
      if (ev.rows && ev.rows[0]) logAdd(s, `Голосование: лидирует вариант ${ev.rows[0].label}`);
      break;
    case "cost":
      s.cost = ev.total;
      break;
    case "verdict":
      s.verdict = ev.verdict;
      logAdd(s, `Председатель выбрал вариант ${ev.verdict.winner}`, "k-ok");
      break;
    case "done":
      logAdd(s, `✓ Заседание завершено, отчёты готовы`, "k-ok");
      s.done = ev;
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
  return `background: hsl(${hue} 65% 50% / .22)`;
}

function render(s) {
  if (view !== "session") return;
  const finished = !!(s.done || s.error);

  const modeName = { debate: "Дебаты", quick: "Быстро", evaluate: "Оценка вариантов" }[s.mode] || s.mode;
  $("sMeta").textContent = [modeName, s.mode === "debate" && s.rounds ? `раундов: ${s.rounds}` : "",
    s.members.length ? `участников: ${s.members.length}` : "", s.mock ? "демо" : ""].filter(Boolean).join(" · ");
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
      $("sQueryToggle").hidden = !clipped;
      $("sQueryToggle").textContent = "Показать запрос полностью";
    });
  }
  $("sRefined").textContent = s.refined && s.refined !== s.question ? `Постановка: ${s.refined}` : "";
  $("sCriteria").innerHTML = s.criteria.map((c) => `<span class="chip">${esc(c)}</span>`).join("");
  $("sCost").textContent = money(s.cost);
  $("cancelBtn").hidden = finished;
  $("sError").hidden = !s.error;
  $("sError").textContent = s.error ? `Заседание прервано: ${s.error}` : "";

  // таймлайн
  const skip = new Set(s.mode === "quick" ? ["critique", "revision"] : s.mode === "evaluate" ? ["proposal", "revision"] : []);
  $("timeline").innerHTML = STAGES.map(([key, name]) => {
    const cls = s.stage === key ? "active" : s.doneStages.has(key) ? "done" : skip.has(key) ? "skip" : "";
    const sub = s.stage === key ? (s.stageTitle[key] || "идёт…") : s.doneStages.has(key) ? "готово" : skip.has(key) ? "не нужно" : "ожидание";
    return `<li class="${cls}"><b>${name}</b>${esc(sub)}</li>`;
  }).join("");

  renderActivity(s);

  // участники
  $("memberStatus").hidden = !s.done;
  if (s.done && s.done.stats) {
    $("memberStatus").innerHTML = Object.entries(s.done.stats).map(([n, st]) =>
      `<span class="chip ${st.failures && st.failures.length ? "fail" : "done"}" title="${st.calls} вызовов">${esc(n)} · ${money(st.cost)}</span>`).join("");
  } else {
    const label = { work: "думает", done: "готово", fail: "сбой" };
    $("memberStatus").innerHTML = Object.entries(s.work).map(([n, st]) =>
      `<span class="chip ${st}" title="${label[st]}">${esc(n)}</span>`).join("");
  }

  renderVerdict(s);
  renderTally(s);
  renderProposals(s);
  renderMatrix(s);

  $("failSec").hidden = !s.failures.length;
  $("failures").innerHTML = s.failures.map((f) =>
    `<li><b>${esc(f.member)}</b> (${esc(STAGE_NAME[f.stage] || f.stage)}): ${esc(f.error)}</li>`).join("");
}

const PHASE_TEXT = {
  queued: "в очереди — ждёт свободного слота",
  sent: "запрос отправлен, ждём начала ответа",
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

function renderActivity(s) {
  const box = $("activity");
  const finished = !!(s.done || s.error);
  const live = !finished && (s.stage || !s.stageStart);
  box.hidden = !(live || s.log.length);
  if (box.hidden) return;
  const now = Date.now();

  // текущий этап и прогресс
  const bar = $("stageBar").parentElement;
  if (finished) {
    const secs = s.done && s.done.duration_s != null ? s.done.duration_s : (now - s.startedAt) / 1000;
    $("stageNow").innerHTML = s.error ? `<b>Заседание прервано</b>` : `<b>✓ Заседание завершено</b> за ${fmtSec(secs)}`;
    bar.classList.remove("indeterminate");
    $("stageBar").style.width = "100%";
  } else if (s.saving) {
    $("stageNow").innerHTML = "<b>Готовлю отчёты</b> · PDF, Word и Markdown";
    bar.classList.add("indeterminate");
  } else if (!s.stage) {
    $("stageNow").innerHTML = "<b>Заседание запускается…</b> подключаемся к моделям";
    bar.classList.add("indeterminate");
  } else {
    // считаем по живым событиям: модель готова, как только её ответ принят
    const names = Object.keys(s.work);
    const total = names.length;
    const ready = names.filter((n) => s.work[n] !== "work"
      || (s.calls[n] && s.calls[n].stage === s.stage && ["done", "failed"].includes(s.calls[n].phase))).length;
    const stageSec = s.stageStart ? (now - s.stageStart) / 1000 : 0;
    const who = s.stage === "framing" ? `председатель (${esc(s.chair || "")}) формулирует критерии`
      : s.stage === "verdict" ? `председатель (${esc(s.chair || "")}) пишет итоговое решение`
      : total ? `готово ${ready} из ${total}` : "";
    $("stageNow").innerHTML = `<b>${esc(s.stageTitle[s.stage] || STAGE_NAME[s.stage] || s.stage)}</b>${who ? ` · ${who}` : ""} · ${fmtSec(stageSec)} на этапе`;
    bar.classList.toggle("indeterminate", ready === 0);
    $("stageBar").style.width = total ? `${Math.round((ready / total) * 100)}%` : "0%";
  }

  // строки моделей текущего этапа
  const rows = [];
  if (!finished && s.stage && !s.saving) {
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
  }
  $("workers").innerHTML = rows.join("");
  $("workers").hidden = !rows.length;

  // подсказка, если модель долго молчит
  const slow = finished ? [] : Object.entries(s.calls).filter(([n, c]) => c.stage === s.stage && ["sent", "thinking"].includes(c.phase)
    && (now - c.since) / 1000 > STALL_SEC && s.work[n] === "work").map(([n]) => n);
  const connOk = now - s.lastMsg < 25000;
  $("stallHint").hidden = !slow.length || !connOk;
  if (slow.length) {
    $("stallHint").textContent = `⏳ ${slow.join(", ")} ${slow.length > 1 ? "думают" : "думает"} дольше ${STALL_SEC} с. `
      + "Это нормально для «думающих» моделей (gpt-5, Gemini 2.5 и др.): они рассуждают перед ответом до 1–3 минут. "
      + "Процесс не завис — связь с сервером есть.";
  }

  // связь с сервером
  const conn = $("conn");
  conn.hidden = finished;
  if (!finished) {
    const age = Math.round((now - s.lastMsg) / 1000);
    conn.classList.toggle("bad", !connOk);
    conn.textContent = connOk ? `Связь с сервером есть · последнее событие ${age} с назад`
      : `Нет вестей от сервера ${age} с — переподключаюсь…`;
  }

  // журнал (новые сверху)
  $("logCount").textContent = `(${s.log.length})`;
  if ($("logBox").open || $("log").dataset.n !== String(s.log.length)) {
    $("log").dataset.n = String(s.log.length);
    $("log").innerHTML = s.log.slice().reverse().map((e) =>
      `<li class="${e.kind}"><time>${e.t.toLocaleTimeString("ru-RU")}</time>${esc(e.text)}</li>`).join("");
  }
}

function renderVerdict(s) {
  const v = s.verdict;
  $("verdictSec").hidden = !v;
  if (!v) return;
  const p = s.proposals[v.winner];
  $("vTitle").textContent = `Вариант ${v.winner}${p ? ` — ${p.title}` : ""} (${authorOf(s, v.winner)})`;
  $("vAnswer").innerHTML = md(v.final_answer);
  $("vWhy").innerHTML = md(v.why);
  $("vDissentBox").hidden = !(v.dissent || []).length;
  $("vDissent").innerHTML = (v.dissent || []).map((x) => `<li>${inline(x)}</li>`).join("");
  $("vRisksBox").hidden = !(v.risks || []).length;
  $("vRisks").innerHTML = (v.risks || []).map((x) => `<li>${inline(x)}</li>`).join("");
  $("downloads").hidden = !s.done;
  $("downloads").innerHTML = s.done ? downloadLinks(s.id) : "";
}

function renderTally(s) {
  const ballots = Object.entries(s.ballots);
  $("tallySec").hidden = !s.tally && !ballots.length;
  const win = s.verdict && s.verdict.winner;
  if (s.tally) {
    $("tally").innerHTML = `<thead><tr><th>#</th><th>Вариант</th><th>Автор</th><th>Борда</th><th>Критика</th><th>1-х мест</th></tr></thead><tbody>${
      s.tally.map((r, i) => `<tr class="${r.label === win ? "winner" : ""}">
        <td class="num">${i + 1}</td>
        <td><span class="lbl">${esc(r.label)}</span> ${esc((s.proposals[r.label] || {}).title || "")}</td>
        <td>${esc(authorOf(s, r.label))}</td>
        <td class="num"><div style="display:flex;gap:.5rem;align-items:center"><div class="bar"><i style="width:${(r.borda * 100).toFixed(0)}%"></i></div>${r.borda.toFixed(2)}</div></td>
        <td class="num">${r.avg_score == null ? "—" : r.avg_score.toFixed(1)}</td>
        <td class="num">${r.first_places}/${r.ballots}</td></tr>`).join("")}</tbody>`;
  } else {
    $("tally").innerHTML = "";
  }
  $("ballots").innerHTML = ballots.map(([voter, b]) => `<div class="ballot"><b>${esc(voter)}</b>
    <span class="rank">${b.ranking.map(esc).join(" › ")}</span>${b.rationale ? `<span class="why">${inline(b.rationale)}</span>` : ""}</div>`).join("");
}

function renderProposals(s) {
  const labels = Object.keys(s.proposals).sort();
  $("proposalsSec").hidden = !labels.length;
  const win = s.verdict && s.verdict.winner;
  $("proposals").innerHTML = labels.map((l) => {
    const p = s.proposals[l];
    const list = (name, xs) => (xs && xs.length ? `<div><b>${name}:</b> ${xs.map(inline).join("; ")}</div>` : "");
    const conf = p.confidence != null && s.authors[l] != null
      ? `<div class="conf">Уверенность <div class="bar"><i style="width:${Math.round(p.confidence * 100)}%"></i></div>${Math.round(p.confidence * 100)}%</div>` : "";
    return `<article class="proposal ${l === win ? "win" : ""}">
      <header><span class="lbl">${esc(l)}</span><div><h3 class="clickable" data-full="${esc(l)}">${esc(p.title)}${s.revised[l] ? '<span class="tag">доработан</span>' : ""}${l === win ? ' <span title="Победитель">🏆</span>' : ""}</h3>
      <div class="author">${esc(authorOf(s, l))}</div></div></header>
      <div class="body md">${md(p.summary)}${p.reasoning ? `<p><b>Обоснование:</b> ${inline(p.reasoning)}</p>` : ""}</div>
      <button class="open-full" type="button" data-full="${esc(l)}">Открыть полностью ⤢</button>
      <div class="lists">${list("Плюсы", p.pros)}${list("Риски", p.risks)}</div>
      ${p.changes ? `<div class="changes"><b>Что изменено:</b> ${inline(p.changes)}</div>` : ""}
      ${conf}
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
      const tip = [
        Object.entries(r.scores).map(([k, x]) => `${k}: ${x}`).join(", "),
        ...(r.strengths || []).map((x) => `+ ${x}`),
        ...(r.weaknesses || []).map((x) => `− ${x}`),
      ].join("\n");
      return `<td class="cell" style="${scoreColor(v)}" title="${esc(tip)}">${v.toFixed(1)}</td>`;
    }).join("");
    return `<tr><th scope="row" class="clickable" data-reviewer="${esc(rv)}" title="Открыть все отзывы">${esc(rv)}</th>${cells}</tr>`;
  }).join("");
  const foot = labels.map((l) => {
    if (!colSums[l].length) return "<td></td>";
    const v = avg(colSums[l]);
    return `<td class="cell" style="${scoreColor(v)}">${v.toFixed(1)}</td>`;
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
  if (location.hash !== `#/s/${id}`) history.replaceState(null, "", `#/s/${encodeURIComponent(id)}`);
  markActiveHistory();
  render(s);
  if (window.matchMedia("(max-width: 960px)").matches) window.scrollTo({ top: 0 });
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
    if (ev.type === "done" || ev.type === "error") {
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

$("sQueryToggle").addEventListener("click", () => {
  const b = $("sQueryBody");
  const open = b.classList.toggle("open");
  $("sQueryToggle").textContent = open ? "Свернуть запрос" : "Показать запрос полностью";
});

$("cancelBtn").addEventListener("click", async () => {
  if (!current || !confirm("Остановить заседание? Уже потраченные запросы не вернутся.")) return;
  try { await api(`/api/sessions/${encodeURIComponent(current.id)}/cancel`, { method: "POST" }); } catch (e) { alert(e.message); }
});
$("blind").addEventListener("change", () => current && render(current));

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

function closeModal() {
  if ($("modal").hidden) return;
  $("modal").hidden = true;
  document.body.classList.remove("modal-open");
  if (current && /\/p\/[A-Z]$/.test(location.hash)) history.replaceState(null, "", `#/s/${encodeURIComponent(current.id)}`);
  if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
}

$("mClose").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function proposalHtml(p, s, label) {
  const parts = [`<div class="section">${md(p.summary)}</div>`];
  if (p.reasoning) parts.push(`<h3>Обоснование</h3><div class="section">${md(p.reasoning)}</div>`);
  if (p.pros && p.pros.length) parts.push(`<h3>Плюсы</h3><ul>${p.pros.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.risks && p.risks.length) parts.push(`<h3>Риски</h3><ul>${p.risks.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
  if (p.changes) parts.push(`<div class="changes"><b>Что изменено после критики:</b> ${inline(p.changes)}</div>`);
  if (p.confidence != null && s.authors[label] != null) parts.push(`<p class="muted small">Уверенность автора: ${Math.round(p.confidence * 100)}%</p>`);
  return parts.join("");
}

function reviewHtml(reviewer, r) {
  const scores = Object.entries(r.scores || {}).map(([k, v]) => `<span class="chip">${esc(k)}: <b>${v}</b></span>`).join("");
  return `<div class="review"><b>${esc(reviewer)}</b><div class="scores">${scores}</div>
    ${(r.strengths || []).length ? `<ul class="plus">${r.strengths.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>` : ""}
    ${(r.weaknesses || []).length ? `<ul class="minus">${r.weaknesses.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>` : ""}</div>`;
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
  history.replaceState(null, "", `#/s/${encodeURIComponent(s.id)}/p/${label}`);
  openModal({ eyebrow: `Вариант ${label}`, title: p.title, sub, tabs, foot });
}

function openReviewer(s, reviewer) {
  const cs = s.critiques[reviewer];
  if (!cs) return;
  const html = (cs.reviews || []).map((r) => {
    const p = s.proposals[r.label] || {};
    return reviewHtml(`${r.label}: ${p.title || ""}`, r);
  }).join("");
  openModal({ eyebrow: "Отзывы рецензента", title: reviewer, sub: `Оценил вариантов: ${(cs.reviews || []).length}`, tabs: [{ name: "Отзывы", html }] });
}

$("matrix").addEventListener("click", (e) => {
  const th = e.target.closest("[data-reviewer]");
  if (th && current) openReviewer(current, th.dataset.reviewer);
});

/* ------------------------------------------------------------ широкие таблицы: тень у края, где есть продолжение */

function markScroll(el) {
  const max = el.scrollWidth - el.clientWidth;
  el.classList.toggle("more-right", max > 1 && el.scrollLeft < max - 1);
  el.classList.toggle("more-left", max > 1 && el.scrollLeft > 1);
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
      return;
    }
    openSession(id);
    if (m[2]) pendingProposal = m[2];
  } else if (location.hash === "#/archive") {
    if (view !== "archive") openArchive();
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
  route();
  window.addEventListener("hashchange", route);
})();
