"use strict";
/* «Проекты закупки»: после «Отбора» — письма-запросы, ответы заводов (текст или файлы), цены и документы,
   сравнение, предупреждения, перепроверка. Помощники ($, esc, api, toast, md, showView, go) — из app.js. */

const PR_STATUS = { new: "не писали", sent: "запрос отправлен", replied: "ответил", declined: "отказался", paused: "отложен" };
let prCurrent = null;

const prMock = () => !(typeof config !== "undefined" && config && config.has_key);

async function openProjects() {
  showView("projects");
  go("#/projects");
  const box = $("projBody");
  $("projTitle").textContent = "Проекты закупки";
  $("projMeta").textContent = "";
  box.innerHTML = '<p class="muted">Загружаю…</p>';
  try {
    const d = await api("/api/projects");
    box.innerHTML = d.items.length ? `<table class="sel-table"><thead><tr><th>Проект</th><th>Поставщиков</th><th>Ответили</th><th>Предупреждения</th><th>Изменён</th></tr></thead><tbody>
      ${d.items.map((p) => `<tr><td><a href="#/projects/${encodeURIComponent(p.id)}">${esc(p.title)}</a></td><td>${p.suppliers}</td><td>${p.replied}</td>
        <td>${p.alerts ? `<span class="badge warn">${p.alerts}</span>` : "—"}</td><td class="muted small">${esc((p.updated || "").slice(0, 16).replace("T", " "))}</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">Проектов пока нет. Проект создаётся из итога «Отбора»: кнопка «Создать проект закупки» под таблицей.</p>';
  } catch (e) { box.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

async function openProject(id) {
  showView("projects");
  go(`#/projects/${encodeURIComponent(id)}`);
  $("projBody").innerHTML = '<p class="muted">Загружаю…</p>';
  try {
    prCurrent = await api(`/api/projects/${encodeURIComponent(id)}`);
    prDraw();
  } catch (e) { $("projBody").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

async function createProject(sessionId) {
  try {
    const r = await api("/api/projects", { method: "POST", body: { session: sessionId } });
    toast("Проект закупки создан");
    openProject(r.id);
  } catch (e) { toast(e.message); }
}

async function prSave(body) {
  prCurrent = await api(`/api/projects/${encodeURIComponent(prCurrent.id)}`, { method: "PUT", body });
  prDraw();
}

function prDraw() {
  const p = prCurrent;
  $("projTitle").textContent = p.title;
  $("projMeta").innerHTML = `<a href="#/projects">← Все проекты</a> · из заседания <a href="#/s/${encodeURIComponent(p.source)}">«Отбор»</a>`
    + (p.spent ? ` · потрачено на письма и разбор ответов $${Number(p.spent).toFixed(3)}` : "");
  const L = p.letters || {};
  const sup = (s) => {
    const docs = (s.documents || []).map((d) => `<li><b>${esc(d.kind || "документ")}</b> ${esc(d.number || "")}${d.holder ? ` · на ${esc(d.holder)}` : ""}${d.valid_until ? ` · до ${esc(d.valid_until)}` : ""}
      ${(d.flags || []).map((f) => `<span class="badge ${/истёк|другое/.test(f) ? "bad" : "warn"}">${esc(f)}</span>`).join(" ")}</li>`).join("");
    const reps = (s.replies || []).slice().reverse().map((r) => `<details><summary>${esc((r.at || "").slice(0, 16).replace("T", " "))} · ${esc(r.source)} — ${esc(r.summary || "")}</summary>
      ${(r.items || []).length ? `<table class="sel-table"><thead><tr><th>SKU</th><th>Цена</th><th>За что</th><th>MOQ</th><th>Incoterms</th><th>Срок</th><th>Условия</th></tr></thead><tbody>
        ${r.items.map((i) => `<tr><td>${esc(i.sku)}</td><td>${esc(i.price)} ${esc(i.currency)}</td><td>${esc(i.unit)}</td><td>${esc(i.moq)}</td><td>${esc(i.incoterms)}</td><td>${esc(i.lead_time)}</td><td>${esc(i.note)}</td></tr>`).join("")}</tbody></table>` : ""}
      ${(r.questions || []).length ? `<p class="small"><b>Не ответили / спросили:</b> ${r.questions.map(esc).join("; ")}</p>` : ""}</details>`).join("");
    const cells = Object.entries(s.cells || {}).slice(0, 8).map(([k, c]) => `${esc(k)}: ${esc(c.value)}`).join(" · ");
    const mail = (s.contact || "").match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
    const le = s.letter_en, lz = s.letter_zh;
    return `<article class="card proj-sup" data-sup="${esc(s.id)}">
      <header><h3>${s.rank ? `${s.rank}. ` : ""}${esc(s.name)}</h3><span class="muted small">${s.score != null ? `балл ${String(s.score).replace(".", ",")}` : ""}${s.website ? ` · ${esc(s.website)}` : ""}</span></header>
      ${cells ? `<p class="muted small">${cells}</p>` : ""}
      <div class="row2"><label class="field"><span>Статус</span><select data-f="status">${Object.entries(PR_STATUS).map(([k, t]) => `<option value="${k}" ${s.status === k ? "selected" : ""}>${t}</option>`).join("")}</select>
        ${s.sent_at ? `<small class="hint">запрос отправлен ${esc(s.sent_at.slice(0, 10))}</small>` : ""}</label>
        <label class="field"><span>Контакт</span><input type="text" data-f="contact" value="${esc(s.contact || "")}" placeholder="e-mail, WeChat, телефон"></label></div>
      <label class="field"><span>Заметки</span><textarea data-f="notes" rows="2">${esc(s.notes || "")}</textarea></label>
      ${le ? `<details><summary>Письмо-запрос этому поставщику</summary>
        <div class="proj-letter"><b>EN:</b> ${esc(le.subject)}<pre>${esc(le.body)}</pre><button type="button" class="link small" data-copy="en">копировать</button>
        ${mail ? ` · <a class="small" href="mailto:${esc(mail[0])}?subject=${encodeURIComponent(le.subject)}&body=${encodeURIComponent(le.body)}">открыть в почте</a>` : ""}</div>
        <div class="proj-letter"><b>中文:</b> ${esc(lz.subject)}<pre>${esc(lz.body)}</pre><button type="button" class="link small" data-copy="zh">копировать</button></div></details>` : ""}
      <details ${(s.replies || []).length ? "" : "open"}><summary><b>Ответ завода</b> — вставьте письмо или приложите прайс, PDF, фото сертификата</summary>
        <textarea data-f="reply" rows="4" placeholder="Текст ответа (любой язык)"></textarea>
        <input type="file" data-f="files" multiple>
        <button type="button" class="secondary" data-act="reply">Разобрать ответ</button> <span class="muted small">цены по SKU и документы; около $0,01–0,05</span></details>
      ${reps ? `<h4>Ответы</h4>${reps}` : ""}
      ${docs ? `<h4>Документы</h4><ul>${docs}</ul>` : ""}
      <div class="sel-actions"><button type="button" class="link small" data-act="save">Сохранить</button> · <button type="button" class="link small danger" data-act="remove">убрать из проекта</button></div>
    </article>`;
  };
  const cmp = (p.compare || []).map((c) => `<h4>${esc(c.sku)}</h4>${c.comparable ? "" : '<p class="muted small">Единицы цены у поставщиков разные — сравните вручную.</p>'}
    <table class="sel-table"><thead><tr><th>Поставщик</th><th>Цена</th><th>В ${esc(c.base_currency)}</th><th>За что</th><th>MOQ</th><th>Incoterms</th><th>Срок</th></tr></thead><tbody>
    ${c.rows.map((r) => `<tr class="${r.best ? "sel-top" : ""}"><td>${r.best ? "✓ " : ""}${esc(r.name)}</td><td>${esc(r.price)} ${esc(r.currency)}</td><td>${r.base != null ? r.base : "—"}</td>
      <td>${esc(r.unit)}</td><td>${esc(r.moq)}</td><td>${esc(r.incoterms)}</td><td>${esc(r.lead_time)}</td></tr>`).join("")}</tbody></table>`).join("");
  const diff = p.recheck_diff;
  $("projBody").innerHTML = `
    ${(p.alerts || []).length ? `<div class="alert warn"><b>Внимание:</b><ul>${p.alerts.map((a) => `<li>${esc(a.text)}</li>`).join("")}</ul></div>` : ""}
    <section class="card"><h2>Что запрашиваем</h2>
      <div class="row2"><label class="field"><span>Название проекта</span><input id="prTitle" type="text" value="${esc(p.title)}"></label>
        <label class="field"><span>Компания (подпись в письмах)</span><input id="prCompany" type="text" value="${esc(p.company || "")}"></label></div>
      <p class="muted small">Одинаковые SKU для всех — тогда цены сравнимы.</p>
      <div id="prSkus">${(p.skus || []).map((x, i) => prSkuRow(x, i)).join("")}</div>
      <button type="button" class="link small" id="prAddSku">+ SKU</button>
      <label class="field"><span>Документы до цены (по одному в строке)</span><textarea id="prDocs" rows="3">${esc((p.documents_required || []).join("\n"))}</textarea></label>
      <div class="row2"><label class="field"><span>Валюта сравнения</span><input id="prBase" type="text" value="${esc(p.base_currency || "USD")}" maxlength="3"></label>
        <label class="field"><span>Курсы к ней (например CNY=0,14; EUR=1,08)</span><input id="prRates" type="text" value="${esc(Object.entries(p.rates || {}).map(([k, v]) => `${k}=${String(v).replace(".", ",")}`).join("; "))}"></label></div>
      <div class="sel-actions"><button type="button" class="primary" id="prSaveMain">Сохранить</button>
        <button type="button" class="secondary" id="prRfq">${L.body_en ? "Пересоставить письмо-запрос" : "Составить письмо-запрос"} (EN + 中文)</button>
        <span class="muted small">одно письмо на всех, имя завода подставляется; около $0,01–0,03</span></div>
      ${L.body_en ? `<details><summary>Текст письма (можно править)</summary>
        <label class="field"><span>Тема EN</span><input id="prSubEn" type="text" value="${esc(L.subject_en)}"></label>
        <label class="field"><span>Письмо EN ({supplier} — имя завода)</span><textarea id="prBodyEn" rows="8">${esc(L.body_en)}</textarea></label>
        <label class="field"><span>Тема 中文</span><input id="prSubZh" type="text" value="${esc(L.subject_zh)}"></label>
        <label class="field"><span>Письмо 中文</span><textarea id="prBodyZh" rows="8">${esc(L.body_zh)}</textarea></label>
        <button type="button" class="link small" id="prSaveLetters">Сохранить письмо</button></details>` : ""}
    </section>
    ${cmp ? `<section class="card"><h2>Сравнение цен</h2>${cmp}</section>` : ""}
    <section><h2>Поставщики (${(p.suppliers || []).length})</h2>${(p.suppliers || []).map(sup).join("")}</section>
    <section class="card"><h2>Перепроверка</h2>
      <p class="muted small">Новый «Отбор» по поставщикам проекта с теми же признаками; когда закончится — здесь будет, что изменилось.</p>
      <button type="button" class="secondary" id="prRecheck">Перепроверить таблицу</button>
      ${(p.rechecks || []).length ? `<p class="small">Последняя: <a href="#/s/${encodeURIComponent(p.rechecks[p.rechecks.length - 1].session)}">заседание от ${esc(p.rechecks[p.rechecks.length - 1].at.slice(0, 10))}</a>${p.recheck_done ? "" : " — ещё идёт или не завершено"}</p>` : ""}
      ${diff ? (diff.length ? `<table class="sel-table"><thead><tr><th>Поставщик</th><th>Признак</th><th>Было</th><th>Стало</th></tr></thead><tbody>${diff.map((x) => `<tr><td>${esc(x.supplier)}</td><td>${esc(x.attr)}</td><td>${esc(x.old || "—")}</td><td>${esc(x.new || "—")}${/^https?:\/\//i.test(x.url || "") ? ` <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">↗</a>` : ""}</td></tr>`).join("")}</tbody></table>` : '<p class="small">Изменений нет.</p>') : ""}
    </section>
    <p><button type="button" class="link small danger" id="prDelete">Удалить проект</button></p>`;
  prBind();
}

function prSkuRow(x, i) {
  return `<div class="row3 pr-sku" data-i="${i}"><input type="text" data-k="name" placeholder="SKU, например Omega-3 1000 мг №60" value="${esc(x.name || "")}">
    <input type="text" data-k="spec" placeholder="состав, доза, упаковка, тесты" value="${esc(x.spec || "")}">
    <input type="text" data-k="volumes" placeholder="объёмы: 5 000 / 20 000 / 50 000" value="${esc(x.volumes || "")}"></div>`;
}

function prMainBody() {
  const skus = [...document.querySelectorAll("#prSkus .pr-sku")].map((r) => Object.fromEntries([...r.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])))
    .filter((x) => x.name);
  const rates = {};
  for (const part of $("prRates").value.split(/[;\n]/)) {
    const m = part.match(/([A-Za-z]{3})\s*=\s*([\d.,]+)/);
    if (m) rates[m[1].toUpperCase()] = Number(m[2].replace(",", "."));
  }
  return { title: $("prTitle").value.trim(), company: $("prCompany").value.trim(), skus,
    documents_required: $("prDocs").value.split("\n").map((x) => x.trim()).filter(Boolean), rates, base_currency: $("prBase").value.trim().toUpperCase() || "USD" };
}

function prBind() {
  const p = prCurrent;
  const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; } };
  $("prAddSku").addEventListener("click", () => $("prSkus").insertAdjacentHTML("beforeend", prSkuRow({}, document.querySelectorAll(".pr-sku").length)));
  $("prSaveMain").addEventListener("click", (e) => busy(e.target, async () => { await prSave(prMainBody()); toast("Сохранено"); }));
  $("prRfq").addEventListener("click", (e) => busy(e.target, async () => {
    await prSave(prMainBody());
    prCurrent = await api(`/api/projects/${encodeURIComponent(p.id)}/rfq`, { method: "POST", body: { mock: prMock() } });
    prDraw(); toast("Письмо-запрос готово — оно в карточке каждого поставщика");
  }));
  if ($("prSaveLetters")) $("prSaveLetters").addEventListener("click", (e) => busy(e.target, () => prSave({ letters: {
    subject_en: $("prSubEn").value, body_en: $("prBodyEn").value, subject_zh: $("prSubZh").value, body_zh: $("prBodyZh").value } })));
  $("prRecheck").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await api(`/api/projects/${encodeURIComponent(p.id)}/recheck`, { method: "POST", body: { mock: prMock() } });
    toast("Перепроверка запущена"); location.hash = `#/s/${encodeURIComponent(r.session)}`;
  }));
  $("prDelete").addEventListener("click", async () => {
    if (!confirm("Удалить проект? Письма, ответы и документы пропадут.")) return;
    await api(`/api/projects/${encodeURIComponent(p.id)}`, { method: "DELETE" }); openProjects();
  });
  for (const card of document.querySelectorAll(".proj-sup")) {
    const sid = card.dataset.sup;
    const f = (k) => card.querySelector(`[data-f="${k}"]`);
    const save = () => prSave({ suppliers: { [sid]: { status: f("status").value, contact: f("contact").value, notes: f("notes").value } } });
    card.querySelector('[data-act="save"]').addEventListener("click", (e) => busy(e.target, save));
    f("status").addEventListener("change", () => save().catch((e) => toast(e.message)));
    card.querySelector('[data-act="remove"]').addEventListener("click", (e) => { if (confirm("Убрать поставщика из проекта?")) busy(e.target, () => prSave({ remove: [sid] })); });
    card.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
      const s = p.suppliers.find((x) => x.id === sid);
      const l = b.dataset.copy === "zh" ? s.letter_zh : s.letter_en;
      navigator.clipboard.writeText(`${l.subject}\n\n${l.body}`).then(() => toast("Скопировано"), () => toast("Не удалось скопировать"));
    }));
    card.querySelector('[data-act="reply"]').addEventListener("click", (e) => busy(e.target, async () => {
      const ids = [];
      for (const file of f("files").files) {
        const res = await fetch(`/api/uploads?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.detail || `файл ${file.name} не загрузился`);
        ids.push(d.id);
      }
      prCurrent = await api(`/api/projects/${encodeURIComponent(p.id)}/suppliers/${encodeURIComponent(sid)}/reply`,
        { method: "POST", body: { text: f("reply").value, attachments: ids, mock: prMock() } });
      prDraw(); toast("Ответ разобран");
    }));
  }
}
