"""«Подобрать состав»: какие модели, в каких ролях и в каком режиме лучше задействовать для этого вопроса.

Рекомендация опирается на четыре вида сведений, и у каждой причины указано, на какой:
  catalog   — факты каталога OpenRouter (контекст, предел ответа, рассуждения, картинки, цена, дата выхода);
  history   — ваши заседания (как часто вариант модели побеждал, оценки критиков, сбои);
  reviews   — «досье» модели: обзоры, бенчмарки и известные проблемы, найденные поиском в интернете
              (со ссылками; запоминается на dossier_days дней);
  knowledge — общие знания модели-советника (могут устареть — так и помечается).

Модель-советник сопоставляет это с вопросом и режимом совета, но выбирает только из кандидатов каталога;
программа проверяет ответ (несуществующие модели, роли, размер состава). Без ключа или при сбое —
подбор по правилам (только каталог и история).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .catalog import created, date, family, is_text_model, modalities, provider, sort_newest
from .config import CouncilConfig

DOSSIER_FILE = "model_dossiers.json"  # в runs/notes/
DOSSIER_BATCH = 6
MODES = ("debate", "quick", "evaluate", "research")
LIGHT = ("flash", "mini", "nano", "lite", "haiku", "small", "air", "tiny")
# известные разработчики: из них берём свежие модели, даже если в совете их нет («какие ещё модели лучше»)
NOTABLE = ("anthropic", "openai", "google", "deepseek", "moonshotai", "x-ai", "qwen", "mistralai", "z-ai", "meta-llama")
SHORT_NAME = {"anthropic": "claude", "openai": "chatgpt", "google": "gemini", "moonshotai": "kimi", "deepseek": "deepseek",
              "x-ai": "grok", "qwen": "qwen", "mistralai": "mistral", "meta-llama": "llama", "perplexity": "perplexity",
              "z-ai": "glm", "minimax": "minimax"}

# Что нужно ролям в каждом режиме — это видит советник (и правила ниже)
MODE_ROLES = {
    "debate": ("Подробное обсуждение: каждый пишет полный вариант, критикует чужие, дорабатывает свой, голосует; "
               "председатель ставит задачу и пишет итог. Ценно разнообразие подходов (разные разработчики) и "
               "сильные рассуждения у всех; председателю нужен большой предел ответа и точное следование формату."),
    "quick": ("Быстрый ответ: варианты и голосование, без критики и доработки. Подходят быстрые недорогие модели; "
              "председатель — надёжная модель с хорошим следованием инструкциям."),
    "evaluate": ("Выбор из вариантов пользователя: модели только критикуют и голосуют. Важны внимательность, "
                 "калибровка оценок, рассуждения; длинный ответ не нужен (кроме председателя)."),
    "research": ("Исследование: подвопросы и углы зрения, варианты с проверяемыми утверждениями, арбитраж утверждений "
                 "поиском (отдельная модель с поиском), доработка, синтез «известно / спорно / неизвестно». Авторам "
                 "нужны длинный контекст, аккуратность с фактами и соблюдение длины; председателю — очень большой "
                 "предел ответа (длинный синтез с таблицами), рассуждения и строгий JSON. Модели, склонные сильно "
                 "превышать длину или обрывать JSON, хуже подходят."),
}
ROLE_TEXT = {"chair": "председатель", "member": "участник", "devil": "адвокат дьявола"}
BASIS = ("catalog", "history", "reviews", "knowledge")


# ------------------------------------------------------------------ схемы ответа советника

class TaskAnalysis(BaseModel):
    kind: str = ""          # тип задачи своими словами
    summary: str = ""       # 1–2 предложения: что нужно получить
    size: str = ""          # small | medium | large — объём работы и итогового ответа
    needs: list[str] = Field(default_factory=list)  # «свежие данные», «длинные документы», «расчёты», «код»…


class Pick(BaseModel):
    model: str
    role: str = "member"    # chair | member | devil
    web_search: bool = False
    why: str = ""
    basis: list[str] = Field(default_factory=list)


class Avoid(BaseModel):
    model: str
    why: str = ""
    basis: list[str] = Field(default_factory=list)


class Advice(BaseModel):
    task: TaskAnalysis = Field(default_factory=TaskAnalysis)
    mode: str = ""
    mode_why: str = ""
    picks: list[Pick] = Field(default_factory=list)
    avoid: list[Avoid] = Field(default_factory=list)
    research_words: int | None = None
    caveats: list[str] = Field(default_factory=list)


class Dossier(BaseModel):
    id: str
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    known_issues: list[str] = Field(default_factory=list)   # обрывы ответа, превышение длины, сбои JSON…
    best_for: list[str] = Field(default_factory=list)       # роли и режимы совета
    benchmarks: list[str] = Field(default_factory=list)     # только с источником
    sources: list[str] = Field(default_factory=list)


class DossierSet(BaseModel):
    models: list[Dossier] = Field(default_factory=list)


# ------------------------------------------------------------------ кандидаты

def facts(m: dict[str, Any]) -> dict[str, Any]:
    pr = m.get("pricing") or {}
    params = set(m.get("supported_parameters") or [])
    mid = m["id"]
    return {"id": mid, "date": date(m), "context": m.get("context_length"),
            "max_out": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "price_in": round(float(pr.get("prompt") or 0) * 1e6, 3),
            "price_out": round(float(pr.get("completion") or 0) * 1e6, 3),
            "reasoning": bool(params & {"reasoning", "include_reasoning"}),
            "image": "image" in modalities(m), "light": any(x in mid.lower() for x in LIGHT),
            "builtin_search": mid.startswith("perplexity/") or mid.endswith(":online"),
            "description": " ".join(str(m.get("description") or "").split())[:500]}


def candidates(cfg: CouncilConfig, models: list[dict[str, Any]], *, per_provider: int = 4,
               extra_providers: int = 2) -> list[dict[str, Any]]:
    """Модели, из которых выбирать: текущие модели участников, свежие модели тех же разработчиков (включая
    старшие и младшие линейки), по паре свежих моделей известных разработчиков, которых в совете нет."""
    text = [m for m in sort_newest(models) if m.get("id") and is_text_model(m)]
    by_id = {m["id"]: m for m in text}
    out: dict[str, dict[str, Any]] = {}
    mine = {provider(m.model) for m in cfg.members}
    for m in cfg.members:
        if m.model in by_id:
            out[m.model] = by_id[m.model]
    for prov in sorted(mine):
        fams: dict[str, int] = {}
        for m in text:
            if provider(m["id"]) != prov or m["id"] in out:
                continue
            f = family(m["id"])
            if fams.get(f, 0) >= 2:  # из каждой линейки — две новейшие версии
                continue
            fams[f] = fams.get(f, 0) + 1
            out[m["id"]] = m
            if sum(1 for x in out if provider(x) == prov) >= per_provider + 1:
                break
    for prov in NOTABLE:
        if prov in mine:
            continue
        for m in [x for x in text if provider(x["id"]) == prov][:extra_providers]:
            out[m["id"]] = m
    rows = [facts(m) for m in out.values()]
    keep = {m.model for m in cfg.members}
    rows.sort(key=lambda r: (r["id"] not in keep, -created(by_id[r["id"]])))
    return rows[:cfg.advisor.max_candidates]


# ------------------------------------------------------------------ досье моделей (поиск)

def load_dossiers(runs_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads((Path(runs_dir) / "notes" / DOSSIER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_dossiers(runs_dir: Path, items: dict[str, dict[str, Any]]) -> None:
    if not items:
        return
    data = load_dossiers(runs_dir)
    data.update(items)
    p = Path(runs_dir) / "notes" / DOSSIER_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def stale(dossiers: dict[str, dict[str, Any]], ids: list[str], days: int, now: float | None = None) -> list[str]:
    now = now or time.time()
    return [i for i in ids if now - float((dossiers.get(i) or {}).get("ts") or 0) > days * 86400]


def dossier_prompt(ids: list[str], today: str) -> str:
    listed = "\n".join(f"- {i}" for i in ids)
    return f"""Today is {today}. Search the web for CURRENT information about each of these AI models (OpenRouter ids):
{listed}

Use official model cards and release notes, independent benchmark leaderboards and evaluations, and credible reviews.
For each model report, briefly and concretely:
- strengths: what it is notably good at (deep reasoning, long-form analysis and research, writing, coding, math,
  following complex instructions and strict JSON, long context use, multilingual / Russian quality, speed, price);
- weaknesses and known_issues: e.g. truncated or overlong answers, ignoring length limits, broken JSON, hallucinated
  sources, refusals, slow or unstable providers;
- best_for: which roles in a multi-model "AI council" it suits (chair who writes a long final synthesis, participant
  writing a full proposal, devil's advocate, fast cheap participant, critic/judge) and for which modes (debate,
  quick answer, research with evidence, judging user options);
- benchmarks: only figures you found in a source, with the benchmark name.
If you found little about a model, say so in weaknesses ("little independent information") — do not guess.
Put the URLs you actually used into "sources".

JSON format:
{{"models": [{{"id": "provider/model", "strengths": ["..."], "weaknesses": ["..."], "known_issues": ["..."],
  "best_for": ["..."], "benchmarks": ["..."], "sources": ["https://..."]}}]}}"""


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", str(u).strip()).rstrip("/").lower()


def clean_dossiers(ds: DossierSet, ids: list[str], citations: list[str], ts: float) -> dict[str, dict[str, Any]]:
    """Досье только для запрошенных моделей; ссылки — только те, что вернул поиск (если он вернул ссылки)."""
    found = {_norm_url(c) for c in citations}
    out = {}
    for d in ds.models:
        if d.id not in ids:
            continue
        srcs = [s for s in d.sources if not found or _norm_url(s) in found or any(_norm_url(s).startswith(f) for f in found)]
        out[d.id] = {**d.model_dump(), "sources": srcs[:6], "ts": ts, "sourced": bool(srcs)}
    return out


# ------------------------------------------------------------------ советник

def advise_prompt(*, question: str, mode: str, attachments: list[dict[str, Any]], followup: bool, budget: float,
                  current: dict[str, Any], cands: list[dict[str, Any]], history: dict[str, dict[str, Any]],
                  dossiers: dict[str, dict[str, Any]], language: str, research_words: int, search_model: str) -> str:
    rows = []
    for c in cands:
        r = {k: v for k, v in c.items() if k != "description"}
        r["developer_says"] = c.get("description", "")[:300]
        h = history.get(c["id"])
        if h:
            r["your_history"] = h
        d = dossiers.get(c["id"])
        if d:
            r["reviews"] = {k: d.get(k) for k in ("strengths", "weaknesses", "known_issues", "best_for", "benchmarks")
                            if d.get(k)}
        rows.append(r)
    modes = "\n".join(f"- {k}: {v}" for k, v in MODE_ROLES.items())
    atts = "; ".join(f"{a.get('name', '')} ({a.get('kind', '')}, ~{a.get('chars', 0)} chars)" for a in attachments) or "none"
    q = question if len(question) <= 7000 else question[:7000] + " …[truncated]"
    return f"""You advise a user of an "AI council" app: several AI models answer one question, critique each other and vote;
a chair frames the task and writes the final answer. Recommend the best lineup and mode for THIS question.

User's question:
<<<
{q}
>>>
Attachments: {atts}. Follow-up to an earlier session: {"yes" if followup else "no"}. Budget limit: ${budget:g}.
Mode chosen by the user now: {mode}. Current lineup: {json.dumps(current, ensure_ascii=False)}.

Council modes and what each role needs:
{modes}
Research mode details: proposal length target {research_words} words; claims are checked by a separate search model
({search_model}) in the arbitration step, so web search for authors is optional (useful for fresh numbers).

Web search for participants: any participant can get web search in the proposal and revision steps (about $0.02 per
request extra). Recommend it ("web_search": true) only where the question needs CURRENT facts (prices, markets, news,
recent releases, regulations) — at most two participants, so the others stay independent. Models with built-in search
(perplexity/…) are not good council participants: they suit fact-checking, which the app already does separately.

Candidate models (choose ONLY from these ids). Fields: catalog facts (context, max_out = answer limit in tokens,
reasoning, image, price per 1M tokens, light = lightweight version), developer_says, your_history (the user's own
sessions: wins, average critic score, failures), reviews (web research about the model, may be partial):
{json.dumps(rows, ensure_ascii=False)}

Rules:
1. First analyse the task: type, expected size of the work and of the final answer, special needs.
2. Pick the mode that fits (the user can keep theirs) and explain why in one sentence.
3. Pick 4-6 models: exactly one "chair", optionally one "devil" (devil's advocate), the rest "member". Prefer
   different developers for diversity of views; prefer the stronger sibling (e.g. pro instead of flash) when the task
   is large or needs deep reasoning; prefer cheap and fast ones only for simple or quick tasks. Respect the budget.
   The chair must have a large answer limit (max_out) and strong reasoning; check context against attachments.
4. For EVERY pick and avoid give a concrete reason tied to this question and mode, and list its basis:
   "catalog" (facts above), "history" (user's sessions), "reviews" (the reviews field), "knowledge" (your own general
   knowledge — use it only when the data above says nothing, it may be outdated). Do not invent benchmark numbers.
5. "avoid": models from the current lineup or the candidates that fit this task worse, with the reason.
6. For research mode you may suggest "research_words" (proposal length) if the default does not fit.
7. "caveats": what you are unsure about (little data on a model, no history yet, etc.).
Write all texts in {language}. Reply with ONE JSON object:
{{"task": {{"kind": "...", "summary": "...", "size": "small|medium|large", "needs": ["..."]}},
 "mode": "debate|quick|evaluate|research", "mode_why": "...",
 "picks": [{{"model": "provider/model", "role": "chair|member|devil", "web_search": false, "why": "...",
            "basis": ["catalog", "history"]}}],
 "avoid": [{{"model": "provider/model", "why": "...", "basis": ["reviews"]}}],
 "research_words": null, "caveats": ["..."]}}"""


def finalize(advice: Advice, cands: list[dict[str, Any]], cfg: CouncilConfig, mode: str) -> Advice:
    """Проверка ответа советника: только кандидаты каталога, один председатель, не больше одного адвоката,
    разумный размер состава. Что исправлено — в оговорках."""
    ids = {c["id"]: c for c in cands}
    notes = list(advice.caveats)
    picks, seen = [], set()
    for p in advice.picks:
        if p.model not in ids:
            notes.append(f"Модель {p.model} не найдена в каталоге OpenRouter — пропущена.")
            continue
        if p.model in seen:
            continue
        seen.add(p.model)
        role = p.role if p.role in ROLE_TEXT else "member"
        basis = [b for b in p.basis if b in BASIS] or ["knowledge"]
        picks.append(p.model_copy(update={"role": role, "basis": basis,
                                          "web_search": bool(p.web_search) and not ids[p.model]["builtin_search"]}))
    chairs = [p for p in picks if p.role == "chair"]
    if not chairs and picks:  # председатель не назначен — тот, у кого больше предел ответа и есть рассуждения
        best = max(picks, key=lambda p: (ids[p.model]["reasoning"], ids[p.model]["max_out"] or 0))
        picks = [p.model_copy(update={"role": "chair"}) if p is best else p for p in picks]
        notes.append(f"Председатель не был назначен — выбран {best.model} (больше предел ответа).")
    elif len(chairs) > 1:
        picks = [p.model_copy(update={"role": "member"}) if p in chairs[1:] else p for p in picks]
    devils = [p for p in picks if p.role == "devil"]
    if len(devils) > 1:
        picks = [p.model_copy(update={"role": "member"}) if p in devils[1:] else p for p in picks]
    if len(picks) > 7:
        notes.append("Советник предложил больше 7 моделей — оставлены первые 7.")
        picks = picks[:7]
    web = [p for p in picks if p.web_search]
    if len(web) > 2:
        picks = [p.model_copy(update={"web_search": False}) if p in web[2:] else p for p in picks]
    avoid = [a.model_copy(update={"basis": [b for b in a.basis if b in BASIS] or ["knowledge"]})
             for a in advice.avoid if a.model not in seen]
    words = advice.research_words if advice.research_words and 300 <= advice.research_words <= 5000 else None
    return advice.model_copy(update={"picks": picks, "avoid": avoid, "caveats": notes,
                                     "mode": advice.mode if advice.mode in MODES else mode, "research_words": words})


# ------------------------------------------------------------------ без советника: правила

_RESEARCH_RE = re.compile(r"исследу|исследован|рынок|рынк|найди|найти|поиск|сравни|конкурент|производител|"
                          r"обзор|research|market|compare|find", re.I)
_FRESH_RE = re.compile(r"актуальн|текущ|сейчас|сегодня|202\d|последн|новост|цен[аыу]|курс|price|latest|current", re.I)


def analyse(question: str, attachments: list[dict[str, Any]]) -> TaskAnalysis:
    n = len(question)
    research = bool(_RESEARCH_RE.search(question))
    size = "large" if n > 2500 or (research and n > 800) else "medium" if n > 400 or research else "small"
    needs = []
    if _FRESH_RE.search(question):
        needs.append("свежие данные из интернета")
    if sum(int(a.get("chars") or 0) for a in attachments) > 60_000:
        needs.append("длинные документы")
    if any(a.get("kind") == "image" for a in attachments):
        needs.append("картинки")
    kind = "исследование" if research else "решение или совет"
    return TaskAnalysis(kind=kind, size=size, needs=needs,
                        summary=f"Объём вопроса — {n} знаков; " + ("нужен широкий поиск и проверка фактов." if research
                                                                   else "нужен обоснованный ответ."))


def _sentence(t: str) -> str:
    return (t[:1].upper() + t[1:]).rstrip(".") + "."


def rule_advice(question: str, mode: str, cfg: CouncilConfig, cands: list[dict[str, Any]],
                history: dict[str, dict[str, Any]], attachments: list[dict[str, Any]]) -> Advice:
    """Подбор по правилам — только факты каталога и ваша история (без ключа, в демо, при сбое советника)."""
    task = analyse(question, attachments)
    big = task.size == "large"
    rec_mode = "research" if task.kind == "исследование" and big and mode != "evaluate" else mode
    by_id = {c["id"]: c for c in cands}
    need_ctx = sum(int(a.get("chars") or 0) for a in attachments) // 3 + 20_000

    def score(c: dict[str, Any]) -> float:
        h = history.get(c["id"]) or {}
        s = 2.0 * c["reasoning"] + (0 if c["light"] else 1.5 if big or rec_mode == "research" else 0.3)
        s += min((c["max_out"] or 8000) / 32000, 2) * (1 if rec_mode in ("research", "debate") else 0.3)
        s += (h.get("win_rate") or 0) * 2 - min(h.get("failures") or 0, 5) * 0.4
        if c["context"] and c["context"] < need_ctx:
            s -= 5
        if c["builtin_search"]:
            s -= 10
        if rec_mode == "quick" and c["light"]:
            s += 1.5
        return s

    picks: list[Pick] = []
    used: set[str] = set()
    for m in [x for x in cfg.members if x.enabled] or cfg.members:  # по одной модели от каждого разработчика совета
        prov = provider(m.model)
        if prov in used:
            continue
        same = [c for c in cands if provider(c["id"]) == prov and not c["builtin_search"]]
        if not same:
            continue
        best = max(same, key=score)
        used.add(prov)
        why = []
        if best["id"] != m.model and m.model in by_id:
            why.append(f"вместо {m.model.split('/')[-1]}: " + ("старшая модель — задача большая"
                       if by_id[m.model]["light"] and not best["light"] else "лучше по рассуждениям и пределу ответа"))
        if best["light"] and (big or rec_mode == "research"):
            why.append("облегчённая — старшей модели у этого разработчика в каталоге нет")
        if best["id"] == m.model:
            why.append("модель из ваших настроек подходит для этой задачи")
        h = history.get(best["id"])
        if h and h.get("proposals"):
            why.append(f"в ваших заседаниях побеждал {h['wins']} из {h['proposals']}")
        picks.append(Pick(model=best["id"], role="member", why=_sentence("; ".join(why)) if why else "",
                          basis=["catalog"] + (["history"] if h else [])))
    if picks:
        cur = next((m.model for m in cfg.members if m.name == cfg.chair), "")
        mine = next((p for p in picks if provider(p.model) == provider(cur)), None)
        ok = mine and by_id[mine.model]["reasoning"] and not by_id[mine.model]["light"] and \
            (by_id[mine.model]["max_out"] or 32000) >= 32000
        chair = mine if ok else max(picks, key=lambda p: (by_id[p.model]["reasoning"], by_id[p.model]["max_out"] or 0,
                                                          -by_id[p.model]["light"]))
        lead = ("Председатель, как в настройках: рассуждает, хватает предела ответа для итога. " if chair is mine
                else "Председатель: самый большой предел ответа и рассуждает — нужно для длинного итога. ")
        picks = [p.model_copy(update={"role": "chair", "why": (lead + p.why).strip()})
                 if p is chair else p for p in picks]
    if "свежие данные из интернета" in task.needs and rec_mode != "research":
        for p in picks:
            if p.role == "member":
                picks = [x.model_copy(update={"web_search": True}) if x is p else x for x in picks]
                break
    picks = [p if p.why or p.role == "chair" else p.model_copy(update={"why": "Лучшая по каталогу у этого разработчика."})
             for p in picks]
    caveats = ["Подбор по правилам: только каталог OpenRouter и ваша история, без анализа вопроса моделью и без обзоров."]
    missing = [m.name for m in cfg.members if m.enabled and not any(provider(c["id"]) == provider(m.model) for c in cands)]
    if missing:
        caveats.append(f"Моделей этих участников нет в каталоге OpenRouter: {', '.join(missing)} — они не рассматривались.")
    if not history:
        caveats.append("Ваших заседаний ещё мало — история моделей не учитывалась.")
    return Advice(task=task, mode=rec_mode, mode_why=("большая задача на поиск и сравнение — режим «Исследование»"
                                                      if rec_mode == "research" and mode != "research" else "режим оставлен как выбран"),
                  picks=picks, caveats=caveats)


# ------------------------------------------------------------------ состав на заседание

def assign(advice: Advice, cfg: CouncilConfig) -> dict[str, Any]:
    """Рекомендованные модели → участники совета на это заседание: тот же участник, если модель у него;
    участник того же разработчика с другой моделью; иначе новый участник (только для этого заседания)."""
    members, models, extra, web = [], {}, [], []
    chair, devil = None, ""
    taken: set[str] = set()
    names = {m.name for m in cfg.members}
    for p in advice.picks:
        slot = next((m.name for m in cfg.members if m.model == p.model and m.name not in taken), None)
        if slot is None:
            slot = next((m.name for m in cfg.members if provider(m.model) == provider(p.model) and m.name not in taken
                         and not m.model.startswith("perplexity/")), None)
            if slot is not None:
                models[slot] = p.model
        if slot is None:
            base = SHORT_NAME.get(provider(p.model), re.sub(r"[^\w.-]", "", provider(p.model))[:20] or "model")
            slot, i = base, 2
            while slot in names:
                slot, i = f"{base}{i}", i + 1
            names.add(slot)
            extra.append({"name": slot, "model": p.model})
        taken.add(slot)
        members.append(slot)
        if p.role == "chair":
            chair = slot
        elif p.role == "devil":
            devil = slot
        if p.web_search:
            web.append(slot)
    return {"members": members, "chair": chair, "devil_advocate": devil, "models": models, "extra_members": extra,
            "web_search": web}
