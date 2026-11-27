/* Интерфейс анализатора БАД: фото → распознавание (Tesseract.js) → проверка (engine.js). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
  const LANGS = [
    ["rus", "Русский"], ["eng", "English"], ["deu", "Deutsch"], ["fra", "Français"],
    ["spa", "Español"], ["ita", "Italiano"], ["chi_sim", "中文"],
  ];
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* приватный режим */ } },
  };

  let engine = null, report = null, filter = "all";
  let worker = null, workerLangs = "";

  // ---------- справочники и движок --------------------------------------------
  const dataReady = Promise.all(["prohibited", "nutrients", "additives", "rules"].map((n) =>
    fetch(`data/${n}.json`).then((r) => { if (!r.ok) throw new Error(n); return r.json(); })))
    .then(([prohibited, nutrients, additives, rules]) => {
      engine = BadEngine.createEngine({ prohibited, nutrients, additives, rules });
    })
    .catch(() => showError("Не удалось загрузить справочники. Обновите страницу."));

  // ---------- элементы --------------------------------------------------------
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "style") e.style.cssText = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const c of kids) if (c != null && c !== false) e.append(c);
    return e;
  }
  function showError(msg) { $("err").replaceChildren(msg ? el("div", { class: "error" }, msg) : ""); }

  // ---------- языки -----------------------------------------------------------
  const chosen = new Set(store.get("langs", ["rus", "eng"]));
  for (const [code, title] of LANGS) {
    const input = el("input", { type: "checkbox", value: code, id: "lang-" + code });
    input.checked = chosen.has(code);
    input.addEventListener("change", () => {
      input.checked ? chosen.add(code) : chosen.delete(code);
      if (!chosen.size) { chosen.add("rus"); $("lang-rus").checked = true; }
      store.set("langs", [...chosen]);
    });
    $("langs").append(el("label", { class: "chip", for: "lang-" + code }, input, el("span", {}, title)));
  }

  // ---------- распознавание фото ---------------------------------------------
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (window.Tesseract) return resolve();
      const s = el("script", { src });
      s.onload = resolve; s.onerror = () => reject(new Error("Не удалось загрузить модуль распознавания. Проверьте интернет."));
      document.head.append(s);
    });
  }
  /** Уменьшить большое фото и перевести в оттенки серого — быстрее и точнее распознаётся. */
  async function prepareImage(file) {
    const bmp = await createImageBitmap(file).catch(() => null);
    const img = bmp || await new Promise((res, rej) => {
      const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(file);
    });
    const max = 2400, w0 = img.width, h0 = img.height;
    const k = Math.min(1, max / Math.max(w0, h0)), k2 = Math.max(w0, h0) < 1200 ? 1200 / Math.max(w0, h0) : k;
    const c = el("canvas"); c.width = Math.round(w0 * k2); c.height = Math.round(h0 * k2);
    const ctx = c.getContext("2d");
    ctx.filter = "grayscale(1) contrast(1.25)";
    ctx.drawImage(img, 0, 0, c.width, c.height);
    return c;
  }
  const STAGES = {
    "loading tesseract core": "Загружаю модуль распознавания…",
    "initializing tesseract": "Запускаю распознавание…",
    "loading language traineddata": "Загружаю языковые данные (один раз)…",
    "initializing api": "Готовлю языки…",
    "recognizing text": "Распознаю текст…",
  };
  async function recognize(file) {
    const status = $("ocr-status"), bar = $("ocr-progress"), fill = bar.firstElementChild;
    $("photo").hidden = false;
    $("photo-img").src = URL.createObjectURL(file);
    bar.hidden = false; fill.style.width = "0%";
    status.textContent = "Готовлю фото…";
    const langs = [...chosen].join("+");
    try {
      await loadScript(TESSERACT_URL);
      if (!worker || workerLangs !== langs) {
        if (worker) await worker.terminate();
        worker = await Tesseract.createWorker([...chosen], 1, {
          logger: (m) => {
            status.textContent = STAGES[m.status] || "Распознаю…";
            if (typeof m.progress === "number") fill.style.width = Math.round(m.progress * 100) + "%";
          },
        });
        workerLangs = langs;
      }
      const canvas = await prepareImage(file);
      const { data } = await worker.recognize(canvas);
      const text = cleanOcr(data.text || "");
      if (!text.trim()) throw new Error("Текст на фото не найден. Снимите этикетку ближе и при хорошем свете.");
      $("text").value = text;
      status.textContent = `Распознано (${Math.round(data.confidence || 0)}% уверенности). Проверьте текст и исправьте ошибки, если они есть.`;
      fill.style.width = "100%";
      run();
    } catch (e) {
      status.textContent = "";
      showError(e.message || String(e));
    } finally {
      setTimeout(() => { bar.hidden = true; }, 600);
    }
  }
  function cleanOcr(t) {
    return t.replace(/­/g, "").replace(/-\n(?=[a-zа-яё])/g, "").replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n").trim();
  }
  for (const id of ["camera", "gallery"]) {
    $(id).addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (f) { showError(""); recognize(f); }
    });
  }

  // ---------- анализ -----------------------------------------------------------
  async function run() {
    showError("");
    await dataReady;
    if (!engine) return;
    const text = $("text").value;
    if (!text.trim()) { showError("Сфотографируйте этикетку или введите состав"); return; }
    const perDay = parseFloat(String($("perday").value).replace(",", "."));
    const product = engine.productFromText(text, {
      units_per_day: isFinite(perDay) ? perDay : 0, amounts_per_unit: $("perunit").checked,
    });
    report = engine.analyze(product);
    filter = "all";
    store.set("draft", { text, perDay: $("perday").value, perUnit: $("perunit").checked });
    render();
    if (matchMedia("(max-width: 899px)").matches) $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("go").addEventListener("click", run);

  const fmtNum = (x) => (x >= 100 ? Math.round(x) : +x.toPrecision(3)).toLocaleString("ru-RU");

  function render() {
    const r = report, c = r.counts;
    const cls = c.critical ? "v-critical" : c.warning ? "v-warning" : "v-ok";
    const over = r.nutrients.filter((n) => n.ratio_upper > 1);
    const nodes = [el("div", { class: "verdict " + cls }, el("b", {}, r.verdict),
      el("small", {}, `Нарушений: ${c.critical} · предупреждений: ${c.warning} · компонентов: ${r.ingredients.length}` +
        (over.length ? ` · превышений нормы: ${over.length}` : "")))];

    if (r.nutrients.length) {
      nodes.push(el("h3", {}, "Суточные дозы"));
      const list = el("div", { class: "doses" });
      for (const n of [...r.nutrients].sort((a, b) => b.ratio_upper - a.ratio_upper)) {
        const isOver = n.ratio_upper > 1;
        list.append(el("div", { class: "dose" + (isOver ? " is-over" : "") },
          el("div", { class: "nm" }, n.name),
          el("div", { class: "amt" }, `${fmtNum(n.daily_amount)} ${n.unit}`),
          el("div", { class: "src" }, n.ingredient),
          el("div", { class: "badge " + (isOver ? "over" : "fine") },
            isOver ? `Норма превышена ${BadEngine.times(n.ratio_upper)}` : `${Math.round(n.pct_upper)}% от допустимого предела`),
          el("div", { class: "meter", title: "Доля от верхнего допустимого уровня" },
            el("i", { class: isOver ? "over" : "", style: `width:${Math.min(100, n.pct_upper)}%` })),
          el("div", { class: "sub" },
            `Предел (ВДУ): ${fmtNum(n.upper)} ${n.unit} · потребность (АУП): ${fmtNum(n.adequate)} ${n.unit} — ` +
            (n.ratio_adequate >= 1.05 ? `выше ${BadEngine.times(n.ratio_adequate)}` : `${Math.round(n.pct_adequate)}%`))));
      }
      nodes.push(list);
    }

    const filters = el("div", { class: "filters" });
    for (const [key, title] of [["all", "Все"], ["critical", "Нарушения"], ["warning", "Внимание"], ["info", "Инфо"], ["ok", "OK"]]) {
      const n = key === "all" ? r.findings.length : c[key];
      if (key !== "all" && !n) continue;
      filters.append(el("button", { type: "button", class: filter === key ? "on" : "",
        onclick: () => { filter = key; render(); } }, `${title} (${n})`));
    }
    nodes.push(el("h3", {}, "Результаты проверки"), filters);
    for (const f of r.findings.filter((f) => filter === "all" || f.level === filter)) {
      nodes.push(el("div", { class: "finding f-" + f.level },
        el("span", { class: "tag" }, BadEngine.LEVEL_LABEL[f.level]), f.message,
        f.basis ? el("div", { class: "basis" }, "Основание: " + f.basis) : null));
    }
    if (r.ingredients.length) {
      const ul = el("ul");
      for (const i of r.ingredients) ul.append(el("li", {}, i.name + (i.amount != null ? ` — ${fmtNum(i.amount)} ${i.unit}` : "")));
      nodes.push(el("details", { class: "ingredients" }, el("summary", {}, `Распознанные компоненты (${r.ingredients.length})`), ul));
    }
    nodes.push(el("div", { class: "actions" },
      el("button", { type: "button", class: "btn ghost", onclick: shareReport }, "Поделиться отчётом"),
      el("button", { type: "button", class: "btn ghost", onclick: downloadReport }, "Скачать (.md)")));
    nodes.push(el("p", { class: "disclaimer" }, r.disclaimer));
    $("result").replaceChildren(...nodes);
  }

  async function shareReport() {
    const md = BadEngine.toMarkdown(report);
    try {
      if (navigator.share) { await navigator.share({ title: "Анализ БАД", text: md }); return; }
      await navigator.clipboard.writeText(md);
      showError(""); $("err").replaceChildren(el("div", { class: "ocr-status" }, "Отчёт скопирован в буфер обмена"));
    } catch (e) { if (e && e.name !== "AbortError") downloadReport(); }
  }
  function downloadReport() {
    const a = el("a", { href: URL.createObjectURL(new Blob([BadEngine.toMarkdown(report)], { type: "text/markdown" })),
      download: "анализ-бад.md" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---------- пример и очистка --------------------------------------------------
  $("example").addEventListener("click", () => {
    $("text").value = [
      "Supplement Facts", "Serving Size: 1 Capsule",
      "Vitamin D3 (as cholecalciferol) 5,000 IU", "Vitamin B6 (as pyridoxine HCl) 25 mg",
      "Zinc (as zinc picolinate) 30 mg", "Ma Huang (Ephedra sinica) extract 100 mg", "Caffeine anhydrous 200 mg",
      "Suggested use: Take 1 capsule daily with food.", "This product cures fatigue.",
    ].join("\n");
    $("perday").value = 1; $("perunit").checked = true;
    run();
  });
  $("clear").addEventListener("click", () => {
    $("text").value = ""; $("perday").value = 1; $("perunit").checked = true;
    $("photo").hidden = true; $("ocr-status").textContent = ""; showError("");
    report = null; store.set("draft", null);
    $("result").replaceChildren(el("div", { class: "empty" }, el("p", {}, "Сфотографируйте этикетку или вставьте состав текстом.")));
  });

  const draft = store.get("draft", null);
  if (draft && draft.text) {
    $("text").value = draft.text; $("perday").value = draft.perDay || 1; $("perunit").checked = draft.perUnit !== false;
    run();
  }

  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
})();
