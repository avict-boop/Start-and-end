// Прогоняет набор этикеток через JS-движок: stdin — JSON-массив кейсов, stdout — результаты.
const fs = require("fs");
const path = require("path");
const { createEngine } = require("../site/js/engine.js");
const dataDir = path.join(__dirname, "..", "bad_checker", "data");
const load = (n) => JSON.parse(fs.readFileSync(path.join(dataDir, n + ".json"), "utf8"));
const engine = createEngine({ prohibited: load("prohibited"), nutrients: load("nutrients"),
  additives: load("additives"), rules: load("rules") });
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const out = cases.map((c) => {
  const p = engine.productFromText(c.text, { units_per_day: c.units_per_day ?? 1,
    label_text: c.label_text ?? c.text });
  const r = engine.analyze(p);
  return { verdict: r.verdict, codes: r.findings.map((f) => f.code).sort(),
    ingredients: r.ingredients.map((i) => [i.name, i.amount, i.unit]),
    nutrients: r.nutrients.map((n) => [n.nutrient_id, +n.daily_amount.toFixed(6)]),
    messages: r.findings.map((f) => f.message).sort() };
});
process.stdout.write(JSON.stringify(out));
