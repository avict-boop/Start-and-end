"""JS-движок (site/js/engine.js) должен давать те же результаты, что и Python-версия."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bad_checker import analyze
from bad_checker.parser import product_from_text

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")

CASES = [
    {"text": (ROOT / "examples" / "fatburner_bad.txt").read_text(encoding="utf-8"), "units_per_day": 2},
    {"text": (ROOT / "examples" / "us_label.txt").read_text(encoding="utf-8")},
    {"text": "Состав:\nВитамин C (аскорбиновая кислота) – 80 мг\nВитамин D3 – 400 МЕ\nЦинк 10 мг\nСелен 55 мкг\n"
             "Биологически активная добавка к пище. Не является лекарственным средством. Рекомендации по применению: "
             "1 таблетка. Противопоказания: беременность. Хранить в сухом месте. Срок годности 2 года. "
             "Изготовитель: ООО «Пример». СГР RU.77.99.11.003.R.000123.01.24"},
    {"text": "Zutaten: Magnesiumcitrat 400 mg; Vitamin B6 (Pyridoxinhydrochlorid) 1,4 mg; Folsäure 200 µg; "
             "Koffein 200 mg. Heilt Müdigkeit. Verzehrempfehlung: 2 Kapseln täglich", "units_per_day": 2},
    {"text": "Ingrédients : vitamine C 1 000 mg ; fer 14 mg ; sélénium 55 µg ; E123"},
    {"text": "Пантотенат кальция 5 мг\nКальция карбонат 500 мг\nВитамин E 1000 МЕ\nМагний 100 мг (в пересчете на магний)\n"
             "Помогает в лечении усталости. СГР RU.77.99.32.003.Е.001234.13.15"},
    {"text": "Ephedra sinica 10 mg; Sibutramine 5 mg; Yohimbine HCl 2 mg; Senna leaf 300 mg; DMAA 50 mg"},
    {"text": "Цинк 25,5 мг; Хром 0,15 мг; Йод 0.3 мг; Витамин A 10000 МЕ", "units_per_day": 1.5},
    {"text": "配料: 维生素C 1000毫克\n锌 30毫克\n麻黄提取物 50毫克\n维生素D3 2000国际单位\n本品可以治疗疲劳。"
             "每日一次，每次两粒。生产商：示例有限公司。保质期：两年。储存于阴凉干燥处。适用人群：成人。"},
    {"text": ""},
]


def python_result(c):
    p = product_from_text(c["text"], units_per_day=c.get("units_per_day", 1))
    r = analyze(p)
    return {"verdict": r.verdict, "codes": sorted(f.code for f in r.findings),
            "ingredients": [[i.name, i.amount, i.unit] for i in p.ingredients],
            "nutrients": [[n.nutrient_id, round(n.daily_amount, 6)] for n in r.nutrients],
            "messages": sorted(f.message for f in r.findings)}


@pytest.mark.skipif(NODE is None, reason="Node.js не установлен")
def test_js_engine_matches_python():
    out = subprocess.run([NODE, str(ROOT / "tests" / "js_runner.js")], input=json.dumps(CASES),
                         capture_output=True, text=True, check=True)
    js = json.loads(out.stdout)
    for case, js_r in zip(CASES, js):
        py_r = python_result(case)
        assert js_r["ingredients"] == py_r["ingredients"], case["text"][:60]
        assert js_r["codes"] == py_r["codes"], case["text"][:60]
        assert js_r["nutrients"] == py_r["nutrients"], case["text"][:60]
        assert js_r["verdict"] == py_r["verdict"]
        assert js_r["messages"] == py_r["messages"], case["text"][:60]
