import asyncio
import json

import pytest

from council.client import Completion, ProviderError
from council.config import CouncilConfig
from council.mock import MockClient
from council.report import render_markdown
from council.schemas import CritiqueSet
from council.session import BudgetExceeded, Council, CouncilError
from council.voting import agreement, extract_json, normalize_critique, normalize_ranking, tally


def make_cfg(**over):
    data = {
        "chair": "a",
        "members": [{"name": n, "model": f"m/{n}"} for n in ("a", "b", "c", "d")],
        "json_retries": 1,
        "chair_proposes": True,  # классическая схема: председатель тоже участник со своим вариантом
    }
    data.update(over)
    return CouncilConfig.model_validate(data)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- parsing / voting


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Вот ответ:\n```json\n{"a": {"b": 2}}\n```\nготово') == {"a": {"b": 2}}
    assert extract_json('мысли {не json} и потом {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("без json")


def test_normalize_ranking():
    assert normalize_ranking(["Вариант B", "a", "B", "Option C"], ["A", "B", "C"]) == ["B", "A", "C"]
    with pytest.raises(ValueError):
        normalize_ranking(["A"], ["A", "B"])


def test_normalize_critique_maps_keys():
    cs = CritiqueSet.model_validate(
        {"reviews": [{"label": "a", "scores": {"цена": 5, "риск": 7}}, {"label": "Z", "scores": {}},
                     {"label": "B", "scores": {"x": 1, "y": 2}}]}
    )
    out = normalize_critique(cs, ["A", "B"], ["Цена", "Риск"])
    assert [r.label for r in out.reviews] == ["A", "B"]
    assert out.reviews[0].scores == {"Цена": 5, "Риск": 7}
    assert out.reviews[1].scores == {"Цена": 1, "Риск": 2}  # сопоставлено по порядку
    with pytest.raises(ValueError):
        normalize_critique(cs, ["A", "B", "C"], ["Цена", "Риск"])


def test_borda_normalized_and_tiebreak():
    ballots = {"a": ["B", "C"], "b": ["C", "A"], "c": ["B", "A"]}
    rows = tally(["A", "B", "C"], ballots, {})
    assert [r.label for r in rows] == ["B", "C", "A"]
    assert rows[0].borda == 1.0 and rows[0].first_places == 2
    assert rows[-1].borda == 0.0


# ---------------------------------------------------------------- full sessions


def test_debate_session_full_flow():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client, seed=1).run("Вопрос?"))
    assert res.mode == "debate"
    assert res.criteria == ["Польза", "Стоимость", "Риски"]
    assert sorted(res.authors.values()) == ["a", "b", "c", "d"]
    assert all("доработан" in p.title for p in res.proposals.values())
    # никто не голосует и не критикует свой вариант
    own = {name: label for label, name in res.authors.items()}
    for voter, b in res.ballots.items():
        assert own[voter] not in b.ranking and len(b.ranking) == 3
    for reviewer, cs in res.critiques.items():
        assert own[reviewer] not in [r.label for r in cs.reviews]
    assert res.verdict.winner == res.tally[0].label
    assert res.total_cost > 0
    assert "Решение совета" in render_markdown(res)
    json.dumps(res.to_dict(), ensure_ascii=False)
    stages = [c["meta"]["stage"] for c in client.calls]
    assert stages.count("proposal") == 4 and stages.count("revision") == 4 and stages[-1] == "verdict"


def test_quick_mode_skips_critique():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client).run("Q", mode="quick", criteria=["x", "y"]))
    stages = {c["meta"]["stage"] for c in client.calls}
    assert stages == {"proposal", "vote", "verdict"}
    assert res.criteria == ["x", "y"] and not res.critiques


def test_evaluate_mode_with_user_options():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client).run("Что выбрать?", options=["Вариант раз", "Вариант два", "Вариант три"]))
    assert res.mode == "evaluate"
    assert list(res.proposals) == ["A", "B", "C"] and set(res.authors.values()) == {None}
    assert all(len(b.ranking) == 3 for b in res.ballots.values())
    assert "revision" not in {c["meta"]["stage"] for c in client.calls}


def test_failed_member_is_dropped_but_session_continues():
    client = MockClient(delay=0, fail_models={"m/b"})
    res = run(Council(make_cfg(), client).run("Q"))
    assert "b" not in res.authors.values() and len(res.proposals) == 3
    assert res.stats["b"].failures


def test_no_quorum():
    cfg = make_cfg(min_quorum=3)
    client = MockClient(delay=0, fail_models={"m/b", "m/c"})
    with pytest.raises(CouncilError, match="кворум"):
        run(Council(cfg, client).run("Q"))


def test_chair_failure_falls_back_to_vote():
    client = MockClient(delay=0, fail_models={"m/a"})
    res = run(Council(make_cfg(), client).run("Q"))
    assert res.verdict.winner == res.tally[0].label
    assert res.criteria  # критерии по умолчанию


class BrokenJsonOnce(MockClient):
    """Первый ответ каждого участника на proposal — мусор; проверяем повтор с подсказкой."""

    def __init__(self):
        super().__init__(delay=0)
        self.seen = set()

    async def complete(self, model, messages, **kw):
        meta = kw.get("meta") or {}
        if meta.get("stage") == "proposal" and model not in self.seen:
            self.seen.add(model)
            return Completion(text="Извините, вот мой ответ без JSON", model=model)
        return await super().complete(model, messages, **kw)


def test_invalid_json_is_retried():
    client = BrokenJsonOnce()
    council = Council(make_cfg(), client)
    res = run(council.run("Q", mode="quick"))
    assert len(res.proposals) == 4
    assert any("error" in e for e in res.transcript)


def test_budget_limit_stops_session():
    cfg = make_cfg(max_cost_usd=0.003)  # mock: $0.001 за вызов
    with pytest.raises(BudgetExceeded):
        run(Council(cfg, MockClient(delay=0)).run("Q"))


def test_config_validation():
    with pytest.raises(ValueError):
        make_cfg(chair="nobody")
    with pytest.raises(ValueError):
        CouncilConfig.model_validate({"chair": "a", "members": [{"name": "a", "model": "x"}]})


def test_load_dotenv(tmp_path, monkeypatch):
    import os
    from council.config import load_dotenv
    for k in ("A1", "A2", "A3", "A4"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("A4", "keep")
    (tmp_path / ".env").write_text('# c\nA1=plain  # comment\nA2="quoted # not comment"\nexport A3=x:y\nA4=override\n')
    load_dotenv(tmp_path / ".env")
    assert (os.environ["A1"], os.environ["A2"], os.environ["A3"], os.environ["A4"]) == ("plain", "quoted # not comment", "x:y", "keep")


def test_extract_json_tolerates_model_quirks():
    # перевод строки внутри строки (частая вольность Gemini) и лишняя запятая
    assert extract_json('```json\n{"summary": "строка 1\nстрока 2", "pros": ["a", "b",],}\n```') == \
        {"summary": "строка 1\nстрока 2", "pros": ["a", "b"]}
    with pytest.raises(ValueError, match="обрезан"):
        extract_json('{"title": "Вариант", "summary": "длинный текст без конца')


class TruncatedOnce(MockClient):
    """Первый ответ на proposal обрезан по лимиту (finish_reason=length)."""

    def __init__(self):
        super().__init__(delay=0)
        self.proposal_calls = []

    async def complete(self, model, messages, **kw):
        meta = kw.get("meta") or {}
        if meta.get("stage") == "proposal":
            self.proposal_calls.append((model, kw["max_tokens"], len(messages)))
            if sum(1 for m, *_ in self.proposal_calls if m == model) == 1:
                return Completion(text='{"title": "Обрыв", "summary": "текст', model=model,
                                  finish_reason="length", cost=0.002)
        return await super().complete(model, messages, **kw)


def test_truncated_json_is_retried_with_bigger_limit():
    client = TruncatedOnce()
    cfg = make_cfg(max_tokens=1000, reasoning_effort="medium")
    res = run(Council(cfg, client).run("Q", mode="quick", criteria=["x"]))
    assert len(res.proposals) == 4
    first, second = [c for c in client.proposal_calls if c[0] == "m/a"]
    assert (first[1], second[1]) == (1000, 2000)   # лимит удвоен
    assert second[2] == first[2] == 2              # обрывок не пересылается обратно
    errors = [e for e in res.transcript if e.get("error")]
    assert errors and "обрезан" in errors[0]["error"] and errors[0]["finish_reason"] == "length"


def test_failed_attempts_cost_is_counted():
    class Expensive(MockClient):
        async def complete(self, model, messages, **kw):
            if model == "m/b":
                raise ProviderError("m/b: пустой ответ", spent=Completion(text="", cost=0.05))
            return await super().complete(model, messages, **kw)

    res = run(Council(make_cfg(), Expensive(delay=0)).run("Q", mode="quick", criteria=["x"]))
    # b упал дважды — на предложении и на голосовании; обе неудачи стоили денег
    assert res.stats["b"].cost == pytest.approx(0.10) and len(res.stats["b"].failures) == 2


def test_call_progress_events():
    events = []
    council = Council(make_cfg(), BrokenJsonOnce(), on_event=lambda e, **d: events.append((e, d)))
    run(council.run("Q", mode="quick", criteria=["x"]))
    calls = [d for e, d in events if e == "call"]
    a = [d["phase"] for d in calls if d["member"] == "a" and d["stage"] == "proposal"]
    # первая попытка вернула не JSON (заглушка без фаз клиента) → проверка → повтор с объяснением → успех
    assert a[0] == "checking" and a[1] == "retry" and a[-1] == "done"
    assert a.index("retry") < a.index("sent") < a.index("thinking") < a.index("writing") < len(a) - 1
    retry = next(d for d in calls if d["member"] == "a" and d["phase"] == "retry")
    assert "формате" in retry["detail"]
    assert all("elapsed" in d for d in calls)


def test_short_title_and_query_formatting():
    from council.reports.html import render_html
    from council.reports.md import render_md
    from council.reports.view import View, short_title
    assert short_title("Как назвать кофейню?") == "Как назвать кофейню?"
    q = "Помоги выбрать стек для магазина. Подробности ниже:\n- бюджет **1 млн**\n- срок 3 месяца\nСпасибо!"
    assert short_title(q) == "Помоги выбрать стек для магазина."
    long_line = "слово " * 40
    assert short_title(long_line).endswith("…") and len(short_title(long_line)) <= 111
    res = run(Council(make_cfg(), MockClient(delay=0)).run(q, mode="quick", criteria=["x"]))
    v = View(res.to_dict())
    html = render_html(v)
    assert "<h1>Помоги выбрать стек для магазина.</h1>" in html
    assert 'class="query"' in html and "<li>бюджет <strong>1 млн</strong></li>" in html and "Спасибо!" in html
    md = render_md(v)
    assert md.startswith("# Помоги выбрать стек для магазина.") and "> - срок 3 месяца  " in md


# ---------------------------------------------------------------- пакет улучшений логики


def test_pairwise_breaks_borda_ties_before_critique():
    # цикл Кондорсе: все равны и по Борда, и попарно — тогда решает оценка критики
    ballots = {"x": ["C", "A", "B"], "y": ["A", "B", "C"], "z": ["B", "C", "A"]}
    crit = {"x": CritiqueSet.model_validate({"reviews": [{"label": "A", "scores": {"k": 2}},
                                                         {"label": "B", "scores": {"k": 9}}]})}
    rows = tally(["A", "B", "C"], ballots, crit)
    by = {r.label: r for r in rows}
    assert by["A"].borda == by["B"].borda == by["C"].borda == 0.5  # цикл Кондорсе: все равны по Борда
    assert by["A"].pairwise == by["B"].pairwise == by["C"].pairwise == 0  # и попарно тоже (A>B, B>C, C>A)
    assert [r.label for r in rows] == ["B", "A", "C"]  # дальше решает критика: B 9 > A 2 > C (нет)
    assert rows[0].tie_break == "critique"

    ballots = {"x": ["A", "B"], "y": ["B", "A"], "z": ["A", "C"], "w": ["C", "B"]}
    rows = tally(["A", "B", "C"], ballots, crit)
    # A: 1, 0, 1 → 0.67; B: 0, 1, 0 → 0.33; C: 0, 1 → 0.5 — без ничьих
    assert [r.label for r in rows] == ["A", "C", "B"] and not any(r.tie_break for r in rows)


def test_pairwise_decides_equal_borda():
    # A и D равны по Борда (0.33), у A даже есть первое место, но большинство общих бюллетеней ставит D выше A
    ballots = {"p": ["A", "B", "D"], "q": ["B", "D", "A"], "r": ["B", "D", "A"]}
    rows = tally(["A", "B", "D"], ballots, {})
    by = {r.label: r for r in rows}
    assert abs(by["A"].borda - by["D"].borda) < 1e-9
    assert by["D"].pairwise > by["A"].pairwise
    assert [r.label for r in rows] == ["B", "D", "A"] and by["D"].tie_break == "pairwise"


def test_agreement_levels():
    same = {"a": ["A", "B", "C"], "b": ["A", "B", "C"], "c": ["A", "B", "C"]}
    assert agreement(same)["tau"] == 1.0 and agreement(same)["level"] == "high"
    opposite = {"a": ["A", "B", "C"], "b": ["C", "B", "A"]}
    assert agreement(opposite)["tau"] == -1.0 and agreement(opposite)["level"] == "low"
    # неполные бюллетени: сравниваются только общие варианты
    partial = {"a": ["B", "C", "D"], "b": ["A", "C", "D"], "c": ["A", "B", "D"]}
    a = agreement(partial)
    assert a["tau"] == 1.0 and a["pairs"] == 3
    assert agreement({"a": ["A"]}) is None


def test_chair_without_own_proposal():
    cfg = make_cfg(chair_proposes=False)
    client = MockClient(delay=0)
    events = []
    res = run(Council(cfg, client, seed=1, on_event=lambda e, **d: events.append((e, d))).run("Вопрос?"))
    stages = {(c["meta"]["member"], c["meta"]["stage"]) for c in client.calls}
    assert ("a", "proposal") not in stages and ("a", "revision") not in stages
    assert ("a", "critique") in stages and ("a", "vote") in stages and ("a", "verdict") in stages
    assert "a" not in res.authors.values() and len(res.proposals) == 3
    assert len(res.ballots["a"].ranking) == 3  # председатель ранжирует все варианты
    assert res.chair_proposed is False and res.to_dict()["agreement"] is not None
    assert any(e == "stage" and d.get("stage") == "proposal" and d.get("chair_proposes") is False for e, d in events)
    # системная инструкция председателя — роль судьи, а не участника со своим вариантом
    from council import prompts
    assert "do not submit a proposal" in prompts.system("a", "Russian", chair=True, judge_only=True)


def test_chair_proposes_when_quorum_needs_it():
    cfg = make_cfg(chair_proposes=False, members=[{"name": n, "model": f"m/{n}"} for n in ("a", "b")])
    client = MockClient(delay=0)
    res = run(Council(cfg, client, seed=1).run("Вопрос?"))
    assert "a" in res.authors.values() and res.chair_proposed is True


def test_prompts_keep_original_request_and_format():
    from council import prompts
    from council.schemas import Proposal
    original = "Сравни варианты.\nФОРМАТ ОТВЕТА: 1) таблица; 2) вывод."
    p = prompts.proposal("Какой вариант лучше?", ["Цена"], None, original)
    assert "ФОРМАТ ОТВЕТА" in p and "follow it exactly" in p
    assert "original request" not in prompts.proposal("Q", ["Цена"], None, "Q")  # не дублируем одинаковое
    props = {"A": Proposal(title="t", summary="s")}
    c = prompts.critique("Какой вариант лучше?", ["Цена"], props, original)
    assert "9-10: excellent" in c and "not length or style" in c and "ФОРМАТ ОТВЕТА" in c
    v = prompts.verdict("Какой вариант лучше?", ["Цена"], props, "1. A: borda=1.00", "", original, "low (tau = 0.10)")
    assert "ФОРМАТ ОТВЕТА" in v and "Do not drop requested sections" in v and "copy them exactly" in v
    assert "Council agreement on the ranking: low" in v


# ---------------------------------------------------------------- пакет 2: бюллетени и проверка фактов


def test_ballot_flags_and_robustness():
    from council.voting import ballot_flags, leader_without
    b = {"claude": ["D", "B", "A", "C"], "chatgpt": ["D", "B", "A"], "gemini": ["D", "B", "C"],
         "kimi": ["C", "A", "B"], "deepseek": ["D", "A", "C"]}
    rows = tally(list("ABCD"), b, {})
    f = ballot_flags(rows, b)
    assert list(f) == ["kimi"] and set(f["kimi"]["flags"]) == {"against", "leader_last"} and f["kimi"]["tau"] == -1.0
    assert leader_without(list("ABCD"), b, {}, set(f)) == "D"
    # согласные бюллетени не помечаются
    assert ballot_flags(rows, {"x": ["A", "B", "C"], "y": ["A", "B", "C"]}) == {}
    # голос, решающий исход: без него лидер меняется
    b2 = {"p": ["A", "B", "C"], "q": ["B", "A", "C"], "r": ["C", "B", "A"]}
    rows2 = tally(list("ABC"), b2, {})
    assert rows2[0].label == "B"
    assert leader_without(list("ABC"), b2, {}, {"r"}) == "A"


def test_fact_check_stage():
    from council.session import FACT_CHECKER, normalize_status
    cfg = make_cfg(chair_proposes=False, fact_check={"enabled": True, "top": 2, "max_claims": 2})
    client = MockClient(delay=0)
    events = []
    res = run(Council(cfg, client, seed=1, on_event=lambda e, **d: events.append((e, d))).run("Вопрос?"))
    calls = [c for c in client.calls if c["meta"]["stage"] == "factcheck"]
    assert len(calls) == 2 and {c["model"] for c in calls} == {"perplexity/sonar-pro"}
    assert list(res.fact_check) == [r.label for r in res.tally[:2]]
    assert all(len(r["claims"]) == 2 for r in res.fact_check.values())  # обрезано до max_claims
    assert res.stats[FACT_CHECKER].calls == 2 and res.fact_checker == "perplexity/sonar-pro"
    order = [d.get("stage") for e, d in events if e == "stage"]
    assert order.index("factcheck") == order.index("vote") + 1 and order[-1] == "verdict"
    verdict_prompt = next(c for c in client.calls if c["meta"]["stage"] == "verdict")
    assert verdict_prompt  # председатель получил итоги проверки (см. test_prompts_include_facts)
    assert normalize_status("Подтверждено") == "confirmed" and normalize_status("FALSE") == "refuted"
    assert normalize_status("maybe") == "unclear"


def test_fact_check_failure_does_not_stop_session():
    class Broken(MockClient):
        async def complete(self, model, messages, **kw):
            if (kw.get("meta") or {}).get("stage") == "factcheck":
                raise ProviderError("поиск недоступен")
            return await super().complete(model, messages, **kw)

    cfg = make_cfg(fact_check={"enabled": True})
    res = run(Council(cfg, Broken(delay=0), seed=1).run("Вопрос?"))
    assert res.fact_check == {} and res.verdict.final_answer
    assert any("factcheck" in f for f in res.stats["факт-чек"].failures)


def test_prompts_include_facts_and_ballot_notes():
    from council import prompts
    from council.schemas import Proposal
    props = {"A": Proposal(title="t", summary="s")}
    facts = prompts.factcheck_block({"A": {"summary": "в целом верно",
                                           "claims": [{"claim": "Х", "status": "refuted", "note": "на деле Y"}]}})
    v = prompts.verdict("Q", ["Цена"], props, "1. A", "", None, "", facts, "ballots diverging: kimi")
    assert "[refuted] Х" in v and "Do not repeat refuted claims" in v and "Ballot check: ballots diverging" in v
    fc = prompts.factcheck("Q", "A", props["A"], 3)
    assert "up to 3 key factual claims" in fc and '"status": "confirmed|refuted|unclear"' in fc


# ---------------------------------------------------------------- ответы «в файле»


class FileAnswerClient(MockClient):
    """Модель m/b сначала «кладёт» предложение в несуществующий файл; times — сколько раз подряд."""

    def __init__(self, times=1, **kw):
        super().__init__(**kw)
        self.times = times
        self.sent = 0

    async def complete(self, model, messages, **kw):
        meta = kw.get("meta") or {}
        if model == "m/b" and meta.get("stage") == "proposal" and self.sent < self.times:
            self.sent += 1
            self.calls.append({"model": model, "meta": meta})
            text = json.dumps({"title": "Исследование рынка", "summary": "Я подготовил подробное исследование и "
                               "сохранил его в файл: [скачать отчёт](sandbox:/mnt/data/research.docx)",
                               "reasoning": "", "pros": [], "risks": [], "confidence": 0.8}, ensure_ascii=False)
            return Completion(text=text, prompt_tokens=10, completion_tokens=10, cost=0.0001, model=model)
        return await super().complete(model, messages, **kw)


def test_file_reference_is_sent_back_for_full_content():
    client = FileAnswerClient(times=1, delay=0)
    res = run(Council(make_cfg(), client, seed=1).run("Вопрос?"))
    label = next(l for l, a in res.authors.items() if a == "b")
    assert "sandbox:" not in res.initial_proposals[label].summary  # повторный ответ принят, ссылка ушла
    tries = [e for e in res.transcript if e["member"] == "b" and e["stage"] == "proposal"]
    assert len(tries) == 2 and "does not exist" in tries[0]["error"]
    assert not res.stats["b"].failures


def test_file_reference_kept_with_warning_after_retries():
    client = FileAnswerClient(times=10, delay=0)
    events = []
    res = run(Council(make_cfg(), client, seed=1, on_event=lambda e, **d: events.append((e, d))).run("Вопрос?"))
    assert "b" in res.authors.values()  # участник не выбывает, но помечен
    assert any("ссылается на файл" in f for f in res.stats["b"].failures)
    assert any(e == "warning" and d["member"] == "b" for e, d in events)


def test_file_ref_detection():
    from council.filerefs import find_file_refs
    assert find_file_refs("Итог: [скачать](sandbox:/mnt/data/report.docx)")
    assert find_file_refs("I've saved the full report as report.xlsx")
    assert not find_file_refs("Как видно из приложенного вами файла, выручка растёт.")
    assert not find_file_refs("Прилагаю таблицу с расчётами:\n| a | b |")
    assert not find_file_refs("Источник: [Росстат](https://rosstat.gov.ru/doc.pdf)")


def test_content_parts_are_joined():
    from council.client import _as_text
    assert _as_text([{"type": "text", "text": "Привет, "}, {"type": "image_url"}, "мир"]) == "Привет, мир"
    assert _as_text(None) == "" and _as_text("x") == "x"


# ---------------------------------------------------------------- доработка с полным сравнением


def test_revision_sees_full_other_proposals_and_compares():
    from council import prompts
    from council.schemas import Proposal
    long = "Начало. " + "подробности " * 200 + "КОНЕЦ_ВАРИАНТА"
    others = {"B": Proposal(title="Другой", summary=long), "C": Proposal(title="Третий", summary="x" * 9000)}
    txt = prompts.revision("Q", ["Цена"], "A", Proposal(title="Мой", summary="s"), [], others, None)
    assert "КОНЕЦ_ВАРИАНТА" in txt  # чужой вариант виден целиком, а не первые 400 символов
    assert "x" * 9000 in txt and "truncated" not in txt  # даже очень длинный — без обрезки
    assert "Compare honestly" in txt and '"position": "kept | improved | switched"' in txt
    assert "That others agree with each other is not a reason by itself" in txt


def test_revision_position_is_normalized_and_saved():
    from council.schemas import Revision
    from council.session import _normalize_revision
    assert _normalize_revision(Revision(title="t", summary="s", position="Switched — rebuilt on B")).position == "switched"
    assert _normalize_revision(Revision(title="t", summary="s", position="???")).position == "improved"
    res = run(Council(make_cfg(), MockClient(delay=0), seed=1).run("Вопрос?"))
    revised = [p for l, p in res.proposals.items() if p is not res.initial_proposals[l]]
    assert revised and all(p.position in ("kept", "improved", "switched") and p.comparison for p in revised)
    d = res.to_dict()
    from council.reports.view import View
    from council.reports.html import render_html
    v = View(d)
    assert v.position_line.startswith("После доработки:")
    html = render_html(v)
    assert "Сравнение с другими вариантами" in html and "Взято у других" in html and "После доработки:" in html
