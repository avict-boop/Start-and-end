/*
 * Движок проверки состава БАД — JavaScript-версия bad_checker (Python).
 * Логика и коды замечаний совпадают с Python-версией; справочники общие
 * (bad_checker/data/*.json). Работает в браузере и в Node.js.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BadEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------- регулярные выражения в стиле Python ----------------------------
  const W = "[\\p{L}\\p{N}_]";
  const UB = `(?:(?<!${W})(?=${W})|(?<=${W})(?!${W}))`;
  /** Python-регэксп (\b, \w — Unicode) → JS RegExp. */
  function pyRe(src, flags = "") {
    let out = "", i = 0;
    while (i < src.length) {
      const c = src[i];
      if (c === "\\" && i + 1 < src.length) {
        const n = src[i + 1];
        if (n === "b") out += UB;
        else if (n === "w") out += W;
        else out += c + n;
        i += 2;
      } else { out += c; i += 1; }
    }
    return new RegExp(out, "u" + flags);
  }
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // ---------- нормализация и разбор ------------------------------------------
  function stripLatinAccents(text) {
    let out = "";
    for (const ch of text) {
      const d = ch.normalize("NFKD");
      out += d.length > 1 && d.charCodeAt(0) < 128 ? d[0] : ch;
    }
    return out.replace(/ß/g, "ss");
  }
  function normalize(text) {
    text = stripLatinAccents(String(text).toLowerCase().replace(/ё/g, "е"));
    text = text.replace(/[–—]/g, "-");
    return text.replace(/\s+/g, " ").trim();
  }

  const UNIT_ALIASES = {
    "мг": "мг", "mg": "мг",
    "мкг": "мкг", "mcg": "мкг", "µg": "мкг", "μg": "мкг", "ug": "мкг",
    "г": "г", "гр": "г", "g": "г",
    "ме": "МЕ", "iu": "МЕ", "me": "МЕ", "ui": "МЕ", "i.e.": "МЕ", "ie": "МЕ",
    "кое": "КОЕ", "cfu": "КОЕ",
    "毫克": "мг", "微克": "мкг", "克": "г", "国际单位": "МЕ",
  };
  const LATIN_UNITS = new Set(["mg", "mcg", "µg", "μg", "ug", "g", "iu", "ui", "ie", "i.e.", "cfu"]);
  const UNIT_RE = Object.keys(UNIT_ALIASES).sort((a, b) => b.length - a.length).map(esc).join("|");
  const AMOUNT_RE = new RegExp(
    `(?<num>(?<![\\p{L}\\d.,])\\d{1,3}(?:[ \\u00a0\\u202f]\\d{3})+(?:[.,]\\d+)?|\\d+(?:[.,]\\d+)*(?:\\s*[x×]\\s*10\\^?\\d+)?)\\s*(?<unit>${UNIT_RE})\\.?(?![а-яa-z])`, "iu");

  function toFloat(num, unit = "") {
    num = num.replace(/[ \u00a0\u202f]/g, "");
    if (LATIN_UNITS.has(unit.toLowerCase()) && /^\d{1,3}(,\d{3})+$/.test(num)) num = num.replace(/,/g, "");
    num = num.replace(/,/g, ".");
    const m = num.match(/^([\d.]+)[x×]10\^?(\d+)/);
    if (m) return parseFloat(m[1]) * 10 ** parseInt(m[2], 10);
    return parseFloat(num);
  }

  const strip = (s, chars) => {
    let a = 0, b = s.length;
    while (a < b && chars.includes(s[a])) a++;
    while (b > a && chars.includes(s[b - 1])) b--;
    return s.slice(a, b);
  };

  function parseIngredient(raw) {
    raw = strip(raw, " \t.;,");
    if (!raw) return null;
    const m = AMOUNT_RE.exec(raw);
    if (!m) return { name: raw, amount: null, unit: null, raw };
    let name = strip(raw.slice(0, m.index), " -:–—(\t");
    if (!name) name = strip(raw.slice(m.index + m[0].length), " -:–—)\t");
    const unit = UNIT_ALIASES[m.groups.unit.toLowerCase()];
    return { name, amount: toFloat(m.groups.num, m.groups.unit), unit, raw };
  }

  function makeTextTools(rules) {
    const head = pyRe(rules.composition_head, "i");
    const section = pyRe(rules.label_section, "i");
    const headSticky = pyRe("^(?:" + rules.composition_head.replace(/^\^/, "") + ")", "i");

    function parseCompositionText(text) {
      const parts = [];
      for (let line of String(text).split(/\r?\n/)) {
        line = line.replace(headSticky, "");
        parts.push(...line.split(";"));
      }
      return parts.map(parseIngredient).filter(Boolean);
    }
    function extractComposition(text) {
      const lines = String(text).split(/\r?\n/);
      const start = lines.findIndex((l) => head.test(l));
      if (start < 0) return text;
      const block = [];
      for (const line of lines.slice(start)) {
        if (block.length && section.test(line)) break;
        block.push(line);
      }
      return block.join("\n");
    }
    return { parseCompositionText, extractComposition };
  }

  // ---------- СГР -------------------------------------------------------------
  const COUNTRIES = { RU: "Россия", BY: "Беларусь", KZ: "Казахстан", AM: "Армения", KG: "Кыргызстан" };
  const REGISTRY_URL = "https://portal.eaeunion.org";
  const SGR_RE = new RegExp(
    String.raw`(?<![\p{L}\p{N}_])(?<country>RU|BY|KZ|AM|KG)\s*\.\s*(?<region>\d{2})\s*\.\s*(?<activity>\d{2})\s*\.\s*` +
    String.raw`(?<kind>\d{2})\s*\.\s*(?<group>\d{3})\s*\.\s*(?<letter>[A-ZА-ЯЁ])\s*\.\s*` +
    String.raw`(?<num>\d{6})\s*\.\s*(?<month>\d{2})\s*\.\s*(?<year>\d{2})(?![\p{L}\p{N}_])`, "giu");
  const LOOSE_RE = /(?<![\p{L}\p{N}_])(RU|BY|KZ|AM|KG)\s*\.\s*\d{2}\s*\.\s*\d{2}[\d.\sA-ZА-Яa-zа-я]{6,30}/iu;

  function findSgr(text) {
    const out = [];
    for (const m of String(text).matchAll(SGR_RE)) {
      const g = m.groups;
      out.push({ raw: m[0].replace(/\s+/g, ""), country: g.country.toUpperCase(), kind: g.kind,
        letter: g.letter.toUpperCase(), number: g.num, month: +g.month, year: 2000 + +g.year });
    }
    return out;
  }
  function sgrProblems(n, today = new Date()) {
    const y = today.getFullYear(), mo = today.getMonth() + 1, out = [];
    if (n.month < 1 || n.month > 12) out.push(`месяц выдачи «${String(n.month).padStart(2, "0")}» некорректен`);
    if (n.year > y || (n.year === y && n.month > mo)) out.push("дата выдачи в будущем");
    if (n.year < 2010) out.push("год выдачи раньше 2010 — свидетельства ЕАЭС выдаются с 2010 г.");
    if (n.kind !== "11") out.push(`код вида продукции «${n.kind}» — для БАД обычно «11»; проверьте, что СГР выдано именно на БАД`);
    return out;
  }
  function malformedSgr(text) {
    SGR_RE.lastIndex = 0;
    if (SGR_RE.test(text)) { SGR_RE.lastIndex = 0; return null; }
    SGR_RE.lastIndex = 0;
    const m = LOOSE_RE.exec(text);
    return m ? m[0].trim() : null;
  }

  // ---------- форматирование ----------------------------------------------------
  function fmt(x) {
    const s = x < 10 ? (Math.floor(x * 1000 + 0.5) / 1000).toFixed(3) : (Math.floor(x * 10 + 0.5) / 10).toFixed(1);
    return s.replace(/0+$/, "").replace(/\.$/, "");
  }
  function times(ratio) {
    if (ratio > 1 && ratio < 1.05) return `на ${Math.max(1, Math.round((ratio - 1) * 100))}%`;
    const r = ratio < 10 ? Math.floor(ratio * 10 + 0.5) / 10 : Math.floor(ratio + 0.5);
    if (r !== Math.trunc(r)) return `в ${r.toFixed(1).replace(".", ",")} раза`;
    const n = Math.trunc(r);
    const word = [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "раза" : "раз";
    return `в ${n} ${word}`;
  }
  const pct = (x) => Math.floor(x + 0.5);

  const LEVEL_ORDER = ["critical", "warning", "info", "ok"];
  const LEVEL_LABEL = { critical: "НАРУШЕНИЕ", warning: "ВНИМАНИЕ", info: "ИНФО", ok: "OK" };
  const DISCLAIMER = "Результат носит справочный характер и не заменяет экспертизу и государственную " +
    "регистрацию (СГР). Справочники требуют сверки с актуальными редакциями нормативных актов.";
  const MASS = { "г": 1e6, "мг": 1e3, "мкг": 1 };

  // ---------- движок ------------------------------------------------------------
  function createEngine(db) {
    const { prohibited, nutrients, additives, rules } = db;
    const text = makeTextTools(rules);
    const minerals = new Set(rules.minerals);

    const stem = (w) => (w.length > 4 ? w.replace(/[аяыиоейьу]+$/u, "") : w);
    function ruPattern(name) {
      const parts = normalize(name).split(" ").map((w) => {
        const st = stem(w);
        return st === w && w.length <= 4 ? esc(w) + "(?![а-яa-z0-9])" : esc(st) + "[а-яa-z]*";
      });
      return new RegExp("(?<![а-яa-z0-9])" + parts.join("[\\s-]+"), "u");
    }
    const latinPattern = (name) => new RegExp("(?<![a-z0-9])" + esc(normalize(name)) + "(?![a-z0-9])", "u");

    const prohibitedPats = prohibited.items.map((item) => [item,
      [...item.names.map(ruPattern), ...item.latin.map(latinPattern)]]);
    const nutrientAliases = nutrients.items
      .flatMap((n) => n.aliases.map((a) => [normalize(a), n]))
      .sort((a, b) => b[0].length - a[0].length)
      .map(([a, n]) => [new RegExp("(?<![а-яa-z])" + esc(a) + "(?![a-z0-9])", "u"), n]);
    const labelReq = rules.label_requirements.map((r) => ({ ...r, res: r.patterns.map((p) => pyRe(p)) }));
    const claims = rules.medical_claims.map((p) => pyRe(p));
    const additiveRes = additives.items.map((item) => [item,
      new RegExp(`(?<![a-zа-я0-9])[eе][\\s-]?${item.code.slice(1)}(?![0-9])`, "u"),
      new RegExp("(?<![а-яa-z])" + esc(normalize(item.name)) + "(?![а-яa-z])", "u")]);

    function findNutrient(name) {
      const t = normalize(name);
      for (const [re, n] of nutrientAliases) if (re.test(t)) return n;
      return null;
    }
    function convert(amount, unit, n) {
      if (unit === "МЕ") return n.iu_to_unit ? amount * n.iu_to_unit : null;
      if (unit in MASS && n.unit in MASS) return (amount * MASS[unit]) / MASS[n.unit];
      return null;
    }

    function analyze(product) {
      const p = { name: "Без названия", ingredients: [], units_per_day: 1, amounts_per_unit: true,
        unit_name: "порция", label_text: "", ...product };
      const findings = [], nutrientsOut = [];
      const add = (level, code, message, extra = {}) =>
        findings.push({ level, code, message, ingredient: extra.ingredient ?? null, basis: extra.basis ?? null });

      if (!p.ingredients.length) add("critical", "no_ingredients", "Состав не распознан или пуст");
      else if (!(p.units_per_day > 0)) add("critical", "bad_dose", "Суточная доза должна быть больше нуля");
      else {
        for (const ing of p.ingredients) {
          checkProhibited(ing, add);
          checkAdditives(ing, add);
          checkNutrient(ing, p, add, findings, nutrientsOut);
        }
        checkFullText(p, add, findings);
        if (p.label_text) checkLabel(p.label_text, add);
        else add("info", "label_missing", "Текст этикетки не передан — проверка маркировки не выполнялась");
      }
      return buildReport(p, findings, nutrientsOut);
    }

    function checkProhibited(ing, add) {
      const t = normalize(ing.raw || ing.name);
      for (const [item, pats] of prohibitedPats) {
        if (pats.some((re) => re.test(t))) {
          const sev = item.severity || "critical";
          const verb = sev === "critical" ? "запрещён для использования в БАД" : "ограничен — требует проверки";
          add(sev, `prohibited:${item.id}`, `Компонент «${ing.name}» (${item.category}) ${verb}. ${item.note}`,
            { ingredient: ing.name, basis: item.basis });
        }
      }
    }
    function checkAdditives(ing, add) {
      const t = normalize(ing.raw || ing.name);
      for (const [item, codeRe, nameRe] of additiveRes) {
        if (codeRe.test(t) || nameRe.test(t)) {
          add(item.severity || "critical", `additive:${item.code}`,
            `Пищевая добавка ${item.code} (${item.name}): ${item.note}`,
            { ingredient: ing.name, basis: "ТР ТС 029/2012, прил. 2" });
        }
      }
    }
    function checkNutrient(ing, p, add, findings, out) {
      const n = findNutrient(ing.name);
      if (!n) {
        if (ing.amount != null && !findings.some((f) => f.ingredient === ing.name))
          add("info", "unknown_ingredient",
            `«${ing.name}» нет в справочнике нормируемых веществ — проверьте допустимость по СГР/экспертизе`,
            { ingredient: ing.name });
        return;
      }
      if (ing.amount == null || !ing.unit) {
        add("warning", "no_amount", `Для «${ing.name}» (${n.name}) не указано количество — невозможно сверить с ВДУ`,
          { ingredient: ing.name });
        return;
      }
      const value = convert(ing.amount, ing.unit, n);
      if (value == null) {
        add("warning", "unit_mismatch", `Не удалось перевести ${fmt(ing.amount)} ${ing.unit} в ${n.unit} для «${ing.name}»`,
          { ingredient: ing.name });
        return;
      }
      const daily = p.amounts_per_unit ? value * p.units_per_day : value;
      const res = {
        nutrient_id: n.id, name: n.name, ingredient: ing.name, daily_amount: daily, unit: n.unit,
        adequate: n.adequate, upper: n.upper,
        pct_adequate: (100 * daily) / n.adequate, pct_upper: (100 * daily) / n.upper,
        ratio_adequate: daily / n.adequate, ratio_upper: daily / n.upper,
      };
      out.push(res);
      const basis = nutrients._meta.short_source;
      const amountStr = `${fmt(daily)} ${n.unit}/сут`;
      const nameN = normalize(ing.name);
      if (minerals.has(n.id) && rules.compound_markers.some((m) => nameN.includes(m)) &&
          !rules.elemental_markers.some((m) => normalize(ing.raw || "").includes(m)))
        add("warning", "compound_mass",
          `«${ing.name}»: похоже, указана масса соединения, а не элементарного вещества (${n.name}). ` +
          "Нормы установлены для элемента — уточните пересчёт", { ingredient: ing.name });

      const upper = `${fmt(n.upper)} ${n.unit}`;
      if (daily > n.upper) {
        add("critical", `over_upper:${n.id}`,
          `${n.name}: ${amountStr} — верхний допустимый уровень (${upper}) превышен ${times(res.ratio_upper)}; ` +
          `суточная потребность (${fmt(n.adequate)} ${n.unit}) превышена ${times(res.ratio_adequate)}`,
          { ingredient: ing.name, basis });
      } else if (res.pct_adequate < 15) {
        add("info", `low:${n.id}`,
          `${n.name}: ${amountStr} — ${pct(res.pct_adequate)}% адекватного уровня (<15%); ` +
          "заявлять продукт «источником» этого вещества нельзя",
          { ingredient: ing.name, basis: "ТР ТС 022/2011, ст. 4.4, п. 13; Приложение 2" });
      } else {
        const over = res.ratio_adequate >= 1.05 ? `, выше суточной потребности ${times(res.ratio_adequate)}` : "";
        add("ok", `within:${n.id}`,
          `${n.name}: ${amountStr} — ${pct(res.pct_adequate)}% АУП${over}; не превышает ВДУ ${upper} (${pct(res.pct_upper)}% ВДУ)`,
          { ingredient: ing.name, basis });
      }
    }
    function checkFullText(p, add, findings) {
      if (!p.label_text) return;
      const already = new Set(findings.map((f) => f.code));
      const t = normalize(p.label_text);
      for (const [item, pats] of prohibitedPats) {
        const code = `prohibited:${item.id}`;
        if (!already.has(code) && pats.some((re) => re.test(t)))
          add(item.severity || "critical", code,
            `В тексте этикетки упомянут «${item.names.slice(0, 2).join("/")}» — ${item.note}`, { basis: item.basis });
      }
    }
    function checkLabel(label, add) {
      const t = normalize(label);
      const letters = t.match(/\p{L}/gu) || [];
      if (letters.length >= 40 && letters.filter((c) => c >= "а" && c <= "я").length / letters.length < 0.3)
        add("warning", "label_not_russian",
          "Маркировка не на русском языке. Для обращения в РФ информация на этикетке " +
          "должна быть на русском языке (допускается наклейка-перевод)", { basis: "ТР ТС 022/2011, ст. 4.8" });
      for (const r of labelReq) if (!r.res.some((re) => re.test(t))) add("warning", r.code, r.message, { basis: r.basis });
      checkSgr(label, add);
      for (const re of claims) {
        const m = re.exec(t);
        if (m) add("critical", "medical_claim",
          `Недопустимое заявление лечебных свойств: «${m[0]}». БАД не может позиционироваться как лекарственное средство`,
          { basis: rules.medical_claims_basis });
      }
    }
    function checkSgr(label, add) {
      const numbers = findSgr(label);
      if (!numbers.length) {
        const bad = malformedSgr(label);
        if (bad) add("warning", "sgr_malformed", `Номер СГР «${bad}» не соответствует формату RU.77.99.11.003.R.000000.01.24`,
          { basis: "Решение Комиссии ТС № 299, гл. I; ТР ТС 021/2011, ст. 24" });
        return;
      }
      for (const n of numbers) {
        const problems = sgrProblems(n), country = COUNTRIES[n.country] || n.country;
        const mm = String(n.month).padStart(2, "0");
        if (problems.length)
          add("warning", "sgr_suspicious", `СГР ${n.raw} (${country}, ${mm}.${n.year}): ${problems.join("; ")}`,
            { basis: "ТР ТС 021/2011, ст. 24" });
        else
          add("info", "sgr_found", `СГР ${n.raw} (${country}, выдано ${mm}.${n.year}) — формат корректен. ` +
            `Действительность сверьте в Едином реестре ЕАЭС: ${REGISTRY_URL}`, { basis: "ТР ТС 021/2011, ст. 24" });
      }
    }

    function buildReport(p, findings, nutrientsOut) {
      const sorted = [...findings].sort((a, b) => LEVEL_ORDER.indexOf(a.level) - LEVEL_ORDER.indexOf(b.level));
      const counts = Object.fromEntries(LEVEL_ORDER.map((l) => [l, findings.filter((f) => f.level === l).length]));
      const verdict = counts.critical ? "НЕ СООТВЕТСТВУЕТ" : counts.warning ? "ТРЕБУЕТ ПРОВЕРКИ"
        : "СООТВЕТСТВУЕТ (по проверенным критериям)";
      return {
        product: p.name, verdict, counts,
        ingredients: p.ingredients.map((i) => ({ name: i.name, amount: i.amount, unit: i.unit })),
        nutrients: nutrientsOut, findings: sorted, disclaimer: DISCLAIMER,
      };
    }

    function productFromText(src, opts = {}) {
      return {
        name: opts.name || "Без названия",
        ingredients: text.parseCompositionText(text.extractComposition(src)),
        units_per_day: opts.units_per_day ?? 1,
        amounts_per_unit: opts.amounts_per_unit ?? true,
        label_text: opts.label_text ?? src,
      };
    }

    return { analyze, productFromText, findNutrient, ...text };
  }

  function toMarkdown(r) {
    const out = [`# Анализ БАД: ${r.product}`, "", `**Итог:** ${r.verdict}`, ""];
    if (r.nutrients.length) {
      out.push("| Вещество | Доза в сутки | % АУП | % ВДУ | Превышение ВДУ |", "|---|---:|---:|---:|---|");
      for (const n of r.nutrients)
        out.push(`| ${n.name} | ${+n.daily_amount.toPrecision(4)} ${n.unit} | ${pct(n.pct_adequate)}% | ${pct(n.pct_upper)}% | ` +
          `${n.ratio_upper > 1 ? "**" + times(n.ratio_upper) + "**" : "—"} |`);
      out.push("");
    }
    out.push("## Результаты проверки", "");
    for (const f of r.findings) out.push(`- **${LEVEL_LABEL[f.level]}** — ${f.message}${f.basis ? ` _(${f.basis})_` : ""}`);
    out.push("", `> ${r.disclaimer}`);
    return out.join("\n");
  }

  return { createEngine, normalize, parseIngredient, findSgr, sgrProblems, times, toMarkdown, LEVEL_LABEL };
});
