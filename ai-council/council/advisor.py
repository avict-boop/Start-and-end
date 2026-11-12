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

from pydantic import BaseModel, Field, field_validator

from .catalog import created, date, family, is_light, is_text_model, modalities, provider, sort_newest
from .config import CouncilConfig

DOSSIER_FILE = "model_dossiers.json"  # в runs/notes/
DOSSIER_BATCH = 6
MODES = ("debate", "quick", "evaluate", "research", "creative")
# известные разработчики: из них берём свежие модели, даже если в совете их нет («какие ещё модели лучше»)
NOTABLE = ("anthropic", "openai", "google", "deepseek", "moonshotai", "x-ai", "qwen", "mistralai", "z-ai", "meta-llama")
SHORT_NAME = {"anthropic": "claude", "openai": "chatgpt", "google": "gemini", "moonshotai": "kimi", "deepseek": "deepseek",
              "x-ai": "grok", "qwen": "qwen", "mistralai": "mistral", "meta-llama": "llama", "perplexity": "perplexity",
              "z-ai": "glm", "minimax": "minimax"}

# Как подбирать модели под режим — советник получает правило для режима, который рекомендует или выбрал человек
MODE_PICK_RULES = {
    "debate": ("strong reasoning and long, well-structured answers for every author; the chair needs a large max_out "
               "and strict format following."),
    "quick": ("fast, inexpensive models are fine (light versions welcome for authors); the chair is a reliable "
              "instruction-follower; deep reasoning is rarely worth its cost here."),
    "evaluate": ("careful judges with calibrated scores and reasoning; long answers are not needed except for the chair."),
    "research": ("long context, factual care, length compliance and strict JSON matter most; the chair needs a very "
                 "large max_out and strong reasoning; avoid models known to ignore length limits or break JSON."),
    "creative": ("CREATIVE mode (names, concepts, ideas, slogans). Optimise for DIVERSITY OF IMAGINATION, not depth of "
                 "reasoning: (a) one model per developer — different labs think differently, this matters more than "
                 "in any other mode; (b) prefer models whose reviews mention creative writing, wordplay, style, "
                 "humour and strong Russian / multilingual quality; (c) reasoning depth is not the goal here: weigh "
                 "for each model whether its hidden reasoning helps or hurts inventiveness for THIS task (it adds time "
                 "and cost and may push towards safe, logical ideas) and decide yourself how many reasoning models to "
                 "include; a light model is acceptable as one author; "
                 "(d) the mode raises temperature, but some reasoning models (e.g. OpenAI o-series / GPT reasoning) "
                 "ignore it — mention that in the reason if you pick one; (e) the chair needs taste and reliable JSON to "
                 "assemble a shortlist from all proposals; a huge max_out is NOT needed (the answer is a shortlist); "
                 "(f) a devil's advocate is useful as a provocateur; (g) web search: only when the task asks to compare "
                 "with existing names, brands or competitors, study current approaches or trends — then give it to "
                 "one or two authors (they study the landscape), the rest stay independent so ideas do not anchor on "
                 "what search found; otherwise no search; (h) a cheaper lineup is usually fine: creativity does not scale "
                 "with price."),
}

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
    "creative": ("Креатив: названия, концепции, идеи, слоганы. У каждого автора своя творческая роль, много идей по "
                 "разным направлениям, критика «да, и…», ремикс, голосование по силе лучших идей, итог — шорт-лист из "
                 "всех вариантов. Без поиска и проверки фактов. Ценнее всего разнообразие моделей разных "
                 "разработчиков и хорошее чувство языка; председателю — вкус и аккуратная сборка шорт-листа."),
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
    confidence: str = ""    # low | medium | high — насколько советник уверен


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
    research_settings: dict[str, Any] = Field(default_factory=dict)  # настройки исследования под этот вопрос
    research_why: dict[str, str] = Field(default_factory=dict)       # почему — по каждой настройке
    research_cost: dict[str, float] = Field(default_factory=dict)    # оценка добавки к цене заседания, $ — по настройке
    baseline: str = ""     # исходная точка: текущий состав и почему от него отклоняемся

    @field_validator("research_cost", mode="before")
    @classmethod
    def _cost(cls, v: Any) -> dict[str, float]:
        out = {}
        for k, x in (v or {}).items() if isinstance(v, dict) else []:
            try:
                out[str(k)] = round(float(str(x).replace("$", "").replace(",", ".")), 3)
            except ValueError:
                continue
        return out
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
            "image": "image" in modalities(m), "light": is_light(mid),
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
- strengths: what it is notably good at (deep reasoning, long-form analysis and research, writing, creative writing
  and wordplay, humour and style, coding, math,
  following complex instructions and strict JSON, long context use, multilingual / Russian quality, speed, price);
- weaknesses and known_issues: e.g. truncated or overlong answers, ignoring length limits, broken JSON, hallucinated
  sources, refusals, slow or unstable providers;
- best_for: which roles in a multi-model "AI council" it suits (chair who writes a long final synthesis, participant
  writing a full proposal, devil's advocate, fast cheap participant, critic/judge) and for which modes (debate,
  quick answer, research with evidence, judging user options, creative ideation: names, concepts, slogans);
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
                  dossiers: dict[str, dict[str, Any]], language: str, research_words: int, search_model: str,
                  research_now: dict[str, Any] | None = None, question_chars: int = 30000,
                  max_claims_cap: int = 30, max_words_cap: int = 2500, history_min: int = 5) -> str:
    rows = []
    for c in cands:
        r = {k: v for k, v in c.items() if k != "description"}
        r["developer_says"] = c.get("description", "")[:300]
        h = history.get(c["id"])
        if h:
            r["your_history"] = {k: v for k, v in h.items() if k != "by_mode"}
            mine = (h.get("by_mode") or {}).get(mode)
            r["your_history_in_this_mode"] = mine or "no sessions in this mode yet"
        d = dossiers.get(c["id"])
        if d:
            r["reviews"] = {k: d.get(k) for k in ("strengths", "weaknesses", "known_issues", "best_for", "benchmarks")
                            if d.get(k)}
        rows.append(r)
    modes = "\n".join(f"- {k}: {v}" for k, v in MODE_ROLES.items())
    pick_rules = f"- {mode}: {MODE_PICK_RULES.get(mode, MODE_PICK_RULES['debate'])}"
    atts = "; ".join(f"{a.get('name', '')} ({a.get('kind', '')}, ~{a.get('chars', 0)} chars)" for a in attachments) or "none"
    q = question if len(question) <= question_chars else question[:question_chars] + " …[truncated]"
    return f"""You advise a user of an "AI council" app: several AI models answer one question, critique each other and vote;
a chair frames the task and writes the final answer. The user has already chosen the mode; do NOT suggest another
mode. Recommend the best lineup (models, roles, web search) for THIS question in THE CHOSEN mode.

User's question:
<<<
{q}
>>>
Attachments: {atts}. Follow-up to an earlier session: {"yes" if followup else "no"}. Budget limit: ${budget:g}.
Mode chosen by the user (fixed): {mode}. Current lineup: {json.dumps(current, ensure_ascii=False)}.

Council modes and what each role needs:
{modes}
How to pick models in the chosen mode:
{pick_rules}

Research mode details: proposal length target {research_words} words; claims are checked by a separate search model
({search_model}) in the arbitration step, so web search for authors is optional (useful for fresh numbers).

Research settings you can tune for THIS question (current values: {json.dumps(research_now or {}, ensure_ascii=False)}):
- arbitration_claims (3-{max_claims_cap}): how many key claims of all proposals the search model checks. More = more confirmed facts
  in "known", cost about $0.02-0.05 per 5 claims. Raise for questions with many entities, numbers, dates, regulations.
- missions (1-2): 2 = one search to confirm and one to refute each batch (stricter), 1 = cheaper.
- fresh_scouts (0-2): authors who search the web already while writing proposals — for current prices, markets,
  regulations, recent releases; proposals may become more similar. 0 for timeless or reasoning-only questions.
- max_words (300-{max_words_cap}): target proposal length. Proposals are material for the chair, who writes the full report;
  raise to 2000-2500 only for catalogue-like tasks with big tables; lower (700-1000) for narrow questions.
- shorten_long (true/false): send far-too-long proposals back once to be shortened (some models ignore length).
- max_claims (3-15): claims per proposal.
- lenses (true/false): a distinct angle for every author (market, technology, economics, sceptic) — off only when the
  question has one narrow angle.
- section_vote (true/false): vote for the best proposal per subquestion — useful for multi-part questions.
- revision_digest (true/false): authors revise from digests of other proposals (cheaper) instead of full texts.
Suggest ONLY settings that should differ from the current values for this question, each with a short reason and
an estimated extra cost of the session in USD in "research_cost" (negative if it saves money). Keep shorten_long on.
Mind the budget.

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
2. Keep the chosen mode "{mode}" — put it into "mode" unchanged; "mode_why" stays empty.
3. Pick 4-6 models: exactly one "chair", optionally one "devil" (devil's advocate), the rest "member". Prefer
   different developers for diversity of views; prefer the stronger sibling (e.g. pro instead of flash) when the task
   is large or needs deep reasoning; prefer cheap and fast ones only for simple or quick tasks. Respect the budget.
   The chair must have a large answer limit (max_out) and strong reasoning; check context against attachments.
4. For EVERY pick and avoid give a concrete reason tied to this question and mode, and list its basis:
   "catalog" (facts above), "history" (user's sessions), "reviews" (the reviews field), "knowledge" (your own general
   knowledge — use it only when the data above says nothing, it may be outdated). Do not invent benchmark numbers.
5. "avoid": models from the current lineup or the candidates that fit this task worse, with the reason.
6. If the chosen mode is research, put tuned settings into "research_settings" and a reason per setting into
   "research_why" (same keys). Leave both empty for other modes or when the current values fit.
7. "caveats": what you are unsure about (little data on a model, no history yet, etc.).
8. First state a baseline in "baseline": the current lineup if it is valid; every deviation needs a benefit for this
   question. "your_history" is shown only for models with at least {history_min} of the user's sessions; it mixes
   all modes, so for wins and scores rely on "your_history_in_this_mode" (results in the mode chosen now) — success
   in research or debate says little about creative ideation and vice versa. Basis
   "knowledge" alone is allowed only with "confidence": "low" and must be mentioned in caveats. General benchmarks
   are not evidence of length compliance, strict JSON or evidence-citation behaviour in this app. Never prefer a model
   because it comes from your own developer. If the question text was truncated, say so in caveats.
Write all texts in {language}. Reply with ONE JSON object:
{{"task": {{"kind": "...", "summary": "...", "size": "small|medium|large", "needs": ["..."]}},
 "mode": "debate|quick|evaluate|research|creative", "mode_why": "...",
 "baseline": "...",
 "picks": [{{"model": "provider/model", "role": "chair|member|devil", "web_search": false, "why": "...",
            "basis": ["catalog", "history"], "confidence": "low|medium|high"}}],
 "avoid": [{{"model": "provider/model", "why": "...", "basis": ["reviews"]}}],
 "research_settings": {{"arbitration_claims": 20}}, "research_why": {{"arbitration_claims": "..."}},
 "research_cost": {{"arbitration_claims": 0.1}},
 "caveats": ["..."]}}"""


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
    final_mode = mode if mode in MODES else (advice.mode if advice.mode in MODES else "debate")  # режим выбирает человек
    rs, why = clean_research(advice.research_settings, advice.research_why, cfg) if final_mode == "research" else ({}, {})
    if words and "max_words" not in rs and words != cfg.research.max_words:
        rs["max_words"] = words
    return advice.model_copy(update={"picks": picks, "avoid": avoid, "caveats": notes, "mode": final_mode, "mode_why": "",
                                     "research_words": rs.get("max_words"), "research_settings": rs,
                                     "research_why": why,
                                     "research_cost": {k: v for k, v in advice.research_cost.items() if k in rs}})


def clean_research(settings: dict[str, Any], why: dict[str, str], cfg: CouncilConfig) -> tuple[dict[str, Any], dict[str, str]]:
    """Только допустимые настройки, в допустимых пределах и отличные от текущих."""
    from pydantic import ValidationError

    from .config import RESEARCH_TUNABLE, ResearchConfig
    cur = cfg.research.model_dump()
    out: dict[str, Any] = {}
    for k, v in (settings or {}).items():
        if k not in RESEARCH_TUNABLE or v is None:
            continue
        try:
            val = getattr(ResearchConfig.model_validate({**cur, k: v}), k)
        except ValidationError:
            continue
        if k == "arbitration_claims":
            val = min(val, cfg.advisor.max_arbitration_claims)
        if k == "max_words":
            val = min(val, cfg.advisor.max_words_cap)
        if val != cur.get(k):
            out[k] = val
    return out, {k: str(why.get(k, "")).strip() for k in out}


# ------------------------------------------------------------------ без советника: правила

_RESEARCH_RE = re.compile(r"исследу|исследован|рынок|рынк|найди|найти|поиск|сравни|конкурент|производител|"
                          r"обзор|research|market|compare|find", re.I)
_CREATIVE_RE = re.compile(r"придума|назван|нейминг|наименован|слоган|концепц|креатив|идеи для|идею для|"
                          r"brand name|naming|slogan|tagline|brainstorm|мозгов[а-я]* штурм", re.I)
_LANDSCAPE_RE = re.compile(r"сравни|конкурент|существующ|аналог|рынк|рынок|тренд|подход|как у других|изучи|"
                           r"competitor|existing|trend|benchmark", re.I)
_FRESH_RE = re.compile(r"актуальн|текущ|сейчас|сегодня|202\d|последн|новост|цен[аыу]|курс|price|latest|current", re.I)


def analyse(question: str, attachments: list[dict[str, Any]], mode: str = "") -> TaskAnalysis:
    n = len(question)
    creative = mode == "creative" or (not mode and bool(_CREATIVE_RE.search(question)))
    research = bool(_RESEARCH_RE.search(question)) and not creative
    size = "large" if n > 2500 or (research and n > 800) else "medium" if n > 400 or research else "small"
    needs = []
    if _FRESH_RE.search(question):
        needs.append("свежие данные из интернета")
    if creative and _LANDSCAPE_RE.search(question):
        needs.append("сравнить с существующим")
    if sum(int(a.get("chars") or 0) for a in attachments) > 60_000:
        needs.append("длинные документы")
    if any(a.get("kind") == "image" for a in attachments):
        needs.append("картинки")
    kind = "творческая задача" if creative else "исследование" if research else "решение или совет"
    return TaskAnalysis(kind=kind, size=size, needs=needs,
                        summary=f"Объём вопроса — {n} знаков; " + (
                            "нужны разнообразные смелые идеи, а не проверка фактов." if creative
                            else "нужен широкий поиск и проверка фактов." if research else "нужен обоснованный ответ."))


def _sentence(t: str) -> str:
    return (t[:1].upper() + t[1:]).rstrip(".") + "."


def rule_advice(question: str, mode: str, cfg: CouncilConfig, cands: list[dict[str, Any]],
                history: dict[str, dict[str, Any]], attachments: list[dict[str, Any]]) -> Advice:
    """Подбор по правилам — только факты каталога и ваша история (без ключа, в демо, при сбое советника)."""
    task = analyse(question, attachments, mode)
    big = task.size == "large"
    rec_mode = mode  # режим выбирает человек; советник подбирает модели под него
    creative = rec_mode == "creative"
    by_id = {c["id"]: c for c in cands}
    need_ctx = sum(int(a.get("chars") or 0) for a in attachments) // 3 + 20_000

    def score(c: dict[str, Any]) -> float:
        h = history.get(c["id"]) or {}
        if creative:  # воображение, а не глубина рассуждений: старшая модель лучше, но рассуждения и длина не главное
            s = 0.5 * c["reasoning"] + (0 if c["light"] else 1.0) + min((c["max_out"] or 8000) / 32000, 1) * 0.2
        else:
            s = 2.0 * c["reasoning"] + (0 if c["light"] else 1.5 if big or rec_mode == "research" else 0.3)
            s += min((c["max_out"] or 8000) / 32000, 2) * (1 if rec_mode in ("research", "debate") else 0.3)
        hm = (h.get("by_mode") or {}).get(rec_mode) or {}
        # победы — в этом режиме (если в нём уже есть хотя бы 3 варианта модели), иначе общие, но с половинным весом
        wr = hm.get("win_rate") if (hm.get("proposals") or 0) >= 3 else (h.get("win_rate") or 0) * 0.5
        s += (wr or 0) * 2 - min(h.get("failures") or 0, 5) * 0.4
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
        if creative:  # шорт-лист не длинный: председателю не нужен огромный предел ответа
            ok = bool(mine) and not by_id[mine.model]["light"]
        chair = mine if ok else max(picks, key=lambda p: (by_id[p.model]["reasoning"], by_id[p.model]["max_out"] or 0,
                                                          -by_id[p.model]["light"]))
        lead = ("Председатель, как в настройках: рассуждает, хватает предела ответа для итога. " if chair is mine
                else "Председатель: самый большой предел ответа и рассуждает — нужно для длинного итога. ")
        picks = [p.model_copy(update={"role": "chair", "why": (lead + p.why).strip()})
                 if p is chair else p for p in picks]
    if ("свежие данные из интернета" in task.needs or (creative and "сравнить с существующим" in task.needs)) \
            and rec_mode != "research":
        for p in picks:
            if p.role == "member":
                picks = [x.model_copy(update={"web_search": True}) if x is p else x for x in picks]
                break
    picks = [p if p.why or p.role == "chair" else p.model_copy(update={"why": "Лучшая по каталогу у этого разработчика."})
             for p in picks]
    caveats = ["Подбор по правилам: только каталог OpenRouter и ваша история, без анализа вопроса моделью и без обзоров."]
    if creative:
        caveats.append("Креатив: по одной модели от каждого разработчика ради разнообразия идей; глубина рассуждений "
                       "не главное. Поиск — одному автору, если нужно сравнить с существующим или изучить подходы; "
                       "остальные придумывают независимо. Насколько модель изобретательна, каталог не говорит — "
                       "смотрите на свои заседания в этом режиме.")
    missing = [m.name for m in cfg.members if m.enabled and not any(provider(c["id"]) == provider(m.model) for c in cands)]
    if missing:
        caveats.append(f"Моделей этих участников нет в каталоге OpenRouter: {', '.join(missing)} — они не рассматривались.")
    if not history:
        caveats.append("Ваших заседаний ещё мало — история моделей не учитывалась.")
    rs: dict[str, Any] = {}
    rwhy: dict[str, str] = {}
    rcost: dict[str, float] = {}
    if rec_mode == "research":
        if big and cfg.research.arbitration_claims < 25:
            rs["arbitration_claims"] = 25
            rwhy["arbitration_claims"] = "большая задача-каталог с множеством фактов — проверить больше утверждений"
            rcost["arbitration_claims"] = round((25 - cfg.research.arbitration_claims) / 5 * 0.04, 2)
        if "свежие данные из интернета" in task.needs and cfg.research.fresh_scouts < 1:
            rs["fresh_scouts"] = 1
            rwhy["fresh_scouts"] = "нужны актуальные данные — один автор ищет в интернете уже при написании варианта"
            rcost["fresh_scouts"] = 0.1
        if big and not cfg.research.shorten_long:
            rs["shorten_long"] = True
            rwhy["shorten_long"] = "в больших задачах модели часто сильно превышают длину — длинные варианты на сокращение"
        if task.size == "small" and cfg.research.max_words > 1000:
            rs["max_words"] = 900
            rwhy["max_words"] = "вопрос узкий — короткие варианты дешевле и не теряют сути"
    return Advice(task=task, mode=rec_mode, mode_why="",
                  picks=picks, caveats=caveats, research_settings=rs, research_why=rwhy, research_cost=rcost,
                  research_words=rs.get("max_words"))


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
