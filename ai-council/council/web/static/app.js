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
      out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if (listRe.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      while (i < lines.length && listRe.test(lines[i])) items.push(lines[i++].match(listRe)[2]);
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</${tag}>`);
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

$("newBtn").addEventListener("click", () => {
  closeStream();
  current = null;
  location.hash = "";
  $("session").hidden = true;
  $("empty").hidden = false;
  document.body.classList.remove("has-session");
  markActiveHistory();
  $("question").focus();
});

/* ------------------------------------------------------------ история */

async function loadHistory() {
  let items = [];
  try { items = await api("/api/sessions"); } catch { return; }
  const ul = $("history");
  if (!items.length) { ul.innerHTML = '<li class="muted">Пока пусто</li>'; return; }
  const statusText = { running: "идёт…", error: "ошибка", cancelled: "остановлено" };
  ul.innerHTML = items.map((it) => {
    const date = it.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/, "$3.$2 $4:$5");
    const meta = it.status === "done"
      ? `${date} · ${esc(it.winner || "")}${it.cost != null ? ` · ${money(it.cost)}` : ""}`
      : `${date} · ${statusText[it.status] || it.status}`;
    return `<li class="item" data-id="${esc(it.id)}"><div class="q"><div>${esc(it.question)}</div><div class="meta">${meta}</div></div>
      ${it.status === "running" ? "" : `<button class="del" title="Удалить" data-del="${esc(it.id)}" aria-label="Удалить">×</button>`}</li>`;
  }).join("");
  markActiveHistory();
}

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
    startedAt: Date.now(), expanded: new Set(),
  };
}

function apply(s, ev) {
  switch (ev.type) {
    case "start":
      Object.assign(s, { question: ev.question, mode: ev.mode, rounds: ev.rounds, members: ev.members || [],
        chair: ev.chair || null, mock: !!ev.mock });
      break;
    case "stage": {
      if (s.stage && s.stage !== ev.stage) s.doneStages.add(s.stage);
      s.stage = ev.stage;
      s.stageTitle[ev.stage] = ev.title;
      const who = ["framing", "verdict"].includes(ev.stage) ? [s.chair].filter(Boolean) : s.members;
      s.work = Object.fromEntries(who.map((n) => [n, "work"]));
      break;
    }
    case "framing":
      s.refined = ev.refined_question; s.criteria = ev.criteria;
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
      s.proposals = { ...ev.proposals }; s.authors = ev.authors;
      break;
    case "revisions":
      for (const [l, p] of Object.entries(ev.revised)) { s.proposals[l] = p; s.revised[l] = true; }
      break;
    case "tally":
      s.tally = ev.rows;
      break;
    case "cost":
      s.cost = ev.total;
      break;
    case "verdict":
      s.verdict = ev.verdict;
      break;
    case "done":
      s.done = ev;
      s.cost = ev.total_cost ?? s.cost;
      if (s.stage) s.doneStages.add(s.stage);
      s.stage = null; s.work = {};
      break;
    case "error":
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
  $("empty").hidden = true;
  $("session").hidden = false;
  document.body.classList.add("has-session");
  const finished = !!(s.done || s.error);

  const modeName = { debate: "Дебаты", quick: "Быстро", evaluate: "Оценка вариантов" }[s.mode] || s.mode;
  $("sMeta").textContent = [modeName, s.mode === "debate" && s.rounds ? `раундов: ${s.rounds}` : "",
    s.members.length ? `участников: ${s.members.length}` : "", s.mock ? "демо" : ""].filter(Boolean).join(" · ");
  $("sQuestion").textContent = s.question;
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

  // участники
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
  const link = $("reportLink");
  link.hidden = !s.done;
  link.href = `/api/sessions/${encodeURIComponent(s.id)}/report`;
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
    const open = s.expanded.has(l);
    const list = (name, xs) => (xs && xs.length ? `<div><b>${name}:</b> ${xs.map(inline).join("; ")}</div>` : "");
    const conf = p.confidence != null && s.authors[l] != null
      ? `<div class="conf">Уверенность <div class="bar"><i style="width:${Math.round(p.confidence * 100)}%"></i></div>${Math.round(p.confidence * 100)}%</div>` : "";
    return `<article class="proposal ${l === win ? "win" : ""}">
      <header><span class="lbl">${esc(l)}</span><div><h3>${esc(p.title)}${s.revised[l] ? '<span class="tag">доработан</span>' : ""}${l === win ? ' <span title="Победитель">🏆</span>' : ""}</h3>
      <div class="author">${esc(authorOf(s, l))}</div></div></header>
      <div class="body md ${open ? "open" : ""}">${md(p.summary)}${p.reasoning ? `<p><b>Обоснование:</b> ${inline(p.reasoning)}</p>` : ""}</div>
      <button class="more" type="button" data-expand="${esc(l)}">${open ? "Свернуть" : "Показать полностью"}</button>
      <div class="lists">${list("Плюсы", p.pros)}${list("Риски", p.risks)}</div>
      ${p.changes ? `<div class="changes"><b>Что изменено:</b> ${inline(p.changes)}</div>` : ""}
      ${conf}
    </article>`;
  }).join("");
  // кнопка «Показать полностью» нужна только если текст не помещается
  for (const body of $("proposals").querySelectorAll(".body:not(.open)")) {
    if (body.scrollHeight <= body.clientHeight + 2) {
      body.classList.add("short");
      body.parentElement.querySelector(".more").hidden = true;
    }
  }
}

$("proposals").addEventListener("click", (e) => {
  const b = e.target.closest("[data-expand]");
  if (!b || !current) return;
  const l = b.dataset.expand;
  current.expanded.has(l) ? current.expanded.delete(l) : current.expanded.add(l);
  renderProposals(current);
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
    return `<tr><th scope="row">${esc(rv)}</th>${cells}</tr>`;
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
  requestAnimationFrame(() => { renderQueued = false; if (current) { render(current); tick(); } });
}

function openSession(id) {
  closeStream();
  current = newState(id);
  const s = current;
  if (location.hash !== `#/s/${id}`) history.replaceState(null, "", `#/s/${encodeURIComponent(id)}`);
  markActiveHistory();
  render(s);
  if (window.matchMedia("(max-width: 960px)").matches) window.scrollTo({ top: 0 });
  timer = setInterval(tick, 1000);
  stream = new EventSource(`/api/sessions/${encodeURIComponent(id)}/events`);
  stream.onmessage = (msg) => {
    if (current !== s) return;
    const ev = JSON.parse(msg.data);
    apply(s, ev);
    scheduleRender();
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

$("cancelBtn").addEventListener("click", async () => {
  if (!current || !confirm("Остановить заседание? Уже потраченные запросы не вернутся.")) return;
  try { await api(`/api/sessions/${encodeURIComponent(current.id)}/cancel`, { method: "POST" }); } catch (e) { alert(e.message); }
});
$("blind").addEventListener("change", () => current && render(current));

/* ------------------------------------------------------------ старт */

(async () => {
  try {
    await initForm();
  } catch (e) {
    showFormError(`Не удалось загрузить конфигурацию: ${e.message}`);
  }
  await loadHistory();
  const m = location.hash.match(/^#\/s\/(.+)$/);
  if (m) openSession(decodeURIComponent(m[1]));
})();
