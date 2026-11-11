"""Единое представление заседания для всех форматов отчёта.

Строится из словаря стенограммы (SessionResult.to_dict() или сохранённый .json), поэтому
отчёты можно в любой момент пересобрать и для старых заседаний.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

import markdown as _markdown

MODE_NAME = {"debate": "Дебаты", "quick": "Быстрый", "evaluate": "Оценка вариантов", "research": "Исследование"}
STAGE_NAME = {"framing": "Постановка", "proposal": "Предложения", "critique": "Критика",
              "revision": "Доработка", "vote": "Голосование", "verdict": "Вердикт", "arbitration": "Арбитраж доказательств"}


@dataclass
class View:
    d: dict[str, Any]
    stem: str = ""
    files_dir: Any = None  # папка с вложениями заседания (Path) — для картинок в отчёте

    @property
    def followup_title(self) -> str:
        """Название заседания, к которому это уточнение (или пусто)."""
        thread = self.d.get("thread") or []
        return (thread[-1].get("title") or thread[-1].get("question", "")[:100]) if thread and self.d.get("followup_of") else ""

    @property
    def attachments(self) -> list[dict[str, Any]]:
        return list(self.d.get("attachments") or [])

    @property
    def image_notes(self) -> dict[str, str]:
        return dict(self.d.get("image_notes") or {})

    def attachment_label(self, m: dict[str, Any]) -> str:
        """«отчёт.pdf — PDF, 12 стр., 340 КБ» и т. п."""
        from ..attachments import human_size
        bits = []
        if m.get("kind") == "image":
            bits.append(f"картинка {m.get('width')}×{m.get('height')}" if m.get("width") else "картинка")
        else:
            bits.append(str(m.get("name", "")).rsplit(".", 1)[-1].upper())
            if m.get("pages"):
                bits.append(f"{m['pages']} стр.")
            if m.get("chars"):
                bits.append(f"{m['chars']:,} симв.".replace(",", " ") + (" (обрезан)" if m.get("truncated") else ""))
        bits.append(human_size(int(m.get("size") or 0)))
        return f"{m.get('name', '')} — " + ", ".join(bits)

    def thumb_data_url(self, meta: dict[str, Any], max_side: int = 900) -> str:
        """Уменьшенная копия картинки для отчёта (data URL) или пустая строка."""
        data = self.attachment_bytes(meta)
        if not data:
            return ""
        import base64
        import io
        try:
            from PIL import Image, ImageOps
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            img.thumbnail((max_side, max_side))
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=82)
            return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:  # noqa: BLE001 — битая картинка не должна ломать отчёт
            return ""

    def attachment_bytes(self, meta: dict[str, Any]) -> bytes | None:
        if not self.files_dir:
            return None
        from pathlib import Path
        p = Path(self.files_dir) / str(meta.get("file", ""))
        try:
            return p.read_bytes() if p.is_file() else None
        except OSError:
            return None

    # ------------------------------------------------------------ основное
    @property
    def question(self) -> str:
        return self.d.get("question", "")

    @property
    def title_text(self) -> str:
        """Короткий заголовок отчёта (первая строка запроса)."""
        return short_title(self.question)

    @property
    def question_is_long(self) -> bool:
        return self.question.strip() != self.title_text

    @property
    def question_rest(self) -> str:
        """Запрос без первой фразы, которая уже стоит в заголовке (чтобы не повторять её под ним)."""
        q = self.question.strip()
        t = self.title_text.rstrip("…").strip()
        if t and q.startswith(t):
            return q[len(t):].lstrip(" \n.")
        return q

    @property
    def question_is_prompt(self) -> bool:
        """Запрос — длинная инструкция (роль, разделы, формат), а не короткий вопрос."""
        q = self.question.strip()
        return len(q) > 400 or len([x for x in q.splitlines() if x.strip()]) > 6

    @property
    def display_title(self) -> str:
        """Заголовок отчёта: короткий вопрос как есть; для длинной инструкции — уточнённая постановка председателя.

        Первая строка инструкции («Ты — эксперт по …») не говорит, о чём заседание, а постановка — говорит.
        """
        if self.question_is_prompt and self.refined:
            return short_title(self.refined, 160)
        return self.title_text

    def question_excerpt(self, limit: int = 420) -> str:
        """Начало запроса для первой страницы (целыми абзацами, не длиннее limit символов)."""
        q = self.question.strip()
        if len(q) <= limit:
            return q
        out = ""
        for para in re.split(r"\n\s*\n", q):
            if out and len(out) + len(para) > limit:
                break
            out += ("\n\n" if out else "") + para
        if len(out) > limit * 1.3 or not out:
            out = q[:limit].rsplit(" ", 1)[0]
        return out.rstrip() + " …"

    @property
    def refined(self) -> str:
        r = self.d.get("refined_question") or ""
        return r if r and r != self.question else ""

    @property
    def criteria(self) -> list[str]:
        return list(self.d.get("criteria") or [])

    @property
    def mode(self) -> str:
        return MODE_NAME.get(self.d.get("mode", ""), self.d.get("mode", ""))

    @property
    def date(self) -> str:
        raw = self.d.get("started_at") or ""
        try:
            return datetime.fromisoformat(raw).astimezone().strftime("%d.%m.%Y %H:%M")
        except ValueError:
            m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", self.stem)
            return f"{m[3]}.{m[2]}.{m[1]} {m[4]}:{m[5]}" if m else ""

    @property
    def cost(self) -> float:
        return float(self.d.get("total_cost_usd") or sum((s or {}).get("cost", 0) for s in self.stats.values()))

    @property
    def duration(self) -> str:
        s = float(self.d.get("duration_s") or 0)
        return f"{int(s // 60)} мин {int(s % 60)} с" if s >= 60 else f"{s:.0f} с"

    @property
    def stats(self) -> dict[str, dict]:
        return self.d.get("stats") or {}

    @property
    def members(self) -> list[str]:
        return list(self.stats)

    # ------------------------------------------------------------ варианты и голосование
    @property
    def verdict(self) -> dict[str, Any]:
        return self.d.get("verdict") or {}

    @property
    def winner(self) -> str:
        return self.verdict.get("winner", "")

    @property
    def proposals(self) -> dict[str, dict]:
        return dict(sorted((self.d.get("proposals") or {}).items()))

    def initial(self, label: str) -> dict | None:
        return (self.d.get("initial_proposals") or {}).get(label)

    def revised(self, label: str) -> bool:
        ini = self.initial(label)
        return bool(ini) and ini != self.proposals.get(label)

    def author(self, label: str) -> str:
        a = (self.d.get("authors") or {}).get(label)
        return a or "пользователь"

    def title(self, label: str) -> str:
        return (self.proposals.get(label) or {}).get("title", "")

    @property
    def agreement(self) -> dict | None:
        """Согласие совета; для старых заседаний считается по сохранённым бюллетеням."""
        from ..voting import agreement
        if self.d.get("agreement"):
            return self.d["agreement"]
        return agreement({m: (b or {}).get("ranking", []) for m, b in (self.d.get("ballots") or {}).items()})

    @property
    def agreement_line(self) -> str:
        """«Согласие совета: высокое (τ = 0.67) — пояснение» или пустая строка."""
        from ..voting import AGREEMENT_HINT, agreement_text
        a = self.agreement
        return f"{agreement_text(a)} — {AGREEMENT_HINT[a['level']]}" if a else ""

    @property
    def agreement_low(self) -> bool:
        a = self.agreement
        return bool(a and a.get("level") == "low")

    @property
    def tally(self) -> list[dict]:
        return list(self.d.get("tally") or [])

    # ------------------------------------------------------------ доработка: сравнение и смена позиции

    POSITION_NAME = {"kept": "позиция сохранена", "improved": "доработан в рамках своего подхода",
                     "switched": "подход сменён"}

    def position(self, label: str) -> str:
        """Позиция автора после доработки (только для доработанных вариантов новых заседаний)."""
        p = self.proposals.get(label) or {}
        return p.get("position", "") if self.revised(label) and "comparison" in p else ""

    @property
    def position_line(self) -> str:
        """«После доработки: сохранили позицию 1 · доработали 2 · сменили подход 1»."""
        c = {"kept": 0, "improved": 0, "switched": 0}
        for l in self.proposals:
            pos = self.position(l)
            if pos in c:
                c[pos] += 1
        if not sum(c.values()):
            return ""
        line = (f"После доработки: сохранили позицию — {c['kept']}, доработали свой подход — {c['improved']}, "
                f"сменили подход — {c['switched']}")
        if c["switched"] * 2 >= sum(c.values()) and sum(c.values()) >= 2:
            line += ". Половина и больше сменили подход — варианты могли сблизиться, смотрите особые мнения"
        return line

    # ------------------------------------------------------------ проверка бюллетеней и фактов

    @property
    def ballot_flags(self) -> dict[str, dict]:
        return dict(self.d.get("ballot_flags") or {})

    @property
    def flag_notes(self) -> list[str]:
        """«kimi — ставит на последнее место вариант, который остальные считают лучшим (τ = −0.33)»."""
        from ..voting import FLAG_NAME
        out = []
        for voter, f in self.ballot_flags.items():
            tau = f" (τ = {f['tau']:.2f})" if f.get("tau") is not None else ""
            out.append(f"{voter} — " + "; ".join(FLAG_NAME.get(k, k) for k in f.get("flags", [])) + tau)
        return out

    @property
    def robust_note(self) -> str:
        """Меняется ли лидер без помеченных бюллетеней."""
        if not self.ballot_flags:
            return ""
        lead = self.tally[0].get("label") if self.tally else None
        alt = self.d.get("leader_without_flagged")
        if alt and alt != lead:
            return f"Без этих бюллетеней лидером был бы вариант {alt} — победа зависит от спорных голосов."
        return "Без этих бюллетеней лидер тот же — результат устойчив."

    IDEA_WORD = {"included": "Включена", "rejected": "Отклонена", "not_addressed": "Не рассмотрена"}
    IDEA_SIGN = {"included": "✓", "rejected": "✗", "not_addressed": "—"}

    @property
    def ideas(self) -> list[dict]:
        """Идеи из вариантов, не ставших основой решения, которые критики советовали сохранить, и решение по ним."""
        out = []
        for i in self.d.get("ideas") or []:
            if isinstance(i, dict) and i.get("idea"):
                st = i.get("decision") if i.get("decision") in self.IDEA_WORD else "not_addressed"
                out.append({**i, "decision": st})
        return out

    @property
    def ideas_line(self) -> str:
        """«5 идей: включено 3, отклонено 2» или пустая строка."""
        ideas = self.ideas
        if not ideas:
            return ""
        c = {k: sum(i["decision"] == k for i in ideas) for k in self.IDEA_WORD}
        parts = [f"включено {c['included']}", f"отклонено {c['rejected']}"]
        if c["not_addressed"]:
            parts.append(f"не рассмотрено {c['not_addressed']}")
        n = len(ideas)
        word = "идея" if n % 10 == 1 and n % 100 != 11 else ("идеи" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "идей")
        return f"{n} {word}: " + ", ".join(parts)

    # ------------------------------------------------------------ режим «Исследование»
    CLAIM_WORD = {"confirmed": "Подтверждено", "refuted": "Опровергнуто", "contested_in_sources": "Спорно в источниках",
                  "unclear": "Не удалось проверить", "user_doc": "Из документов пользователя",
                  "search_unavailable": "Поиск недоступен", "not_checked": "Не проверялось"}
    CLAIM_SIGN = {"confirmed": "✓", "refuted": "✗", "contested_in_sources": "⇄", "unclear": "?", "user_doc": "📄",
                  "search_unavailable": "—", "not_checked": "—"}
    BASIS_WORD = {"memory": "из памяти модели", "calc": "расчёт", "user_doc": "из документов", "search_reported": "из поиска"}
    COVER_WORD = {"covered": "раскрыт", "partial": "частично", "unknown": "без ответа"}
    GAP_WORD = {"unverified_memory": "только по памяти моделей", "no_public_data": "нет открытых данных",
                "not_checked": "не проверено"}

    @staticmethod
    def think_note(s: dict) -> str:
        """«рассуждения 12 000» / «рассуждения выключены» — для таблицы расходов."""
        bits = []
        rt = int((s or {}).get("reasoning_tokens") or 0)
        if rt:
            bits.append(f"рассуждения {rt:,}".replace(",", " "))
        if (s or {}).get("reasoning_off"):
            bits.append("рассуждения выключены")
        return ", ".join(bits)

    @property
    def is_research(self) -> bool:
        return self.d.get("mode") == "research" and bool(self.d.get("research"))

    @property
    def research(self) -> dict[str, Any]:
        return dict(self.d.get("research") or {})

    @property
    def subquestions(self) -> list[str]:
        return list(self.research.get("subquestions") or [])

    @property
    def scope_line(self) -> str:
        r = self.research
        bits = [r.get("scope") or ""]
        if r.get("as_of"):
            bits.append(f"актуально на {r['as_of']}")
        return " · ".join(b for b in bits if b)

    @property
    def ledger(self) -> dict[str, Any]:
        return dict(self.research.get("ledger") or {})

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return [e for e in self.ledger.get("evidence") or [] if self.safe_url(e.get("url", ""))]

    @property
    def evidence_by_id(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.evidence}

    @property
    def checked_claims(self) -> list[dict[str, Any]]:
        return list(self.ledger.get("claims") or [])

    @property
    def claim_counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for x in self.checked_claims:
            c[x.get("status", "not_checked")] = c.get(x.get("status", "not_checked"), 0) + 1
        return c

    @property
    def ledger_line(self) -> str:
        """«проверено 10 утверждений: ✓ 4 · ⇄ 2 · ? 4; источников 12» или пусто."""
        claims = self.checked_claims
        if not claims:
            return ""
        c = self.claim_counts
        parts = [f"{self.CLAIM_SIGN[k]} {c[k]}" for k in self.CLAIM_WORD if c.get(k)]
        return (f"проверено {len(claims)} {_plural(len(claims), 'утверждение', 'утверждения', 'утверждений')}: "
                + " · ".join(parts) + f"; источников {len(self.evidence)}")

    def research_list(self, key: str) -> list[dict[str, Any]]:
        return [x for x in (self.verdict.get(key) or []) if isinstance(x, dict)]

    @property
    def known(self) -> list[dict[str, Any]]:
        return self.research_list("known")

    @property
    def contested(self) -> list[dict[str, Any]]:
        return self.research_list("contested")

    @property
    def unknown(self) -> list[dict[str, Any]]:
        return self.research_list("unknown")

    @property
    def coverage_rows(self) -> list[dict[str, Any]]:
        """Подвопросы: текст, статус в итоге, каркас (лидер голосования по подвопросу)."""
        audit = {x.get("subquestion_id"): x for x in self.research_list("coverage_audit")}
        sections = self.research.get("sections") or {}
        backbone = self.verdict.get("section_backbone") or {}
        rows = []
        for i, q in enumerate(self.subquestions, 1):
            qid = f"Q{i}"
            a = audit.get(qid) or {}
            sec = sections.get(qid) or {}
            rows.append({"id": qid, "text": q, "status": a.get("status", ""), "note": a.get("note", ""),
                         "backbone": backbone.get(qid) or sec.get("leader", ""), "counts": sec.get("counts") or {}})
        return rows

    def evidence_refs(self, ids: list[str]) -> list[dict[str, Any]]:
        by = self.evidence_by_id
        return [by[i] for i in ids if i in by]

    @property
    def kcu_line(self) -> str:
        """«известно 3 · спорно 2 · неизвестно 4»."""
        if not self.is_research:
            return ""
        return f"известно {len(self.known)} · спорно {len(self.contested)} · неизвестно {len(self.unknown)}"

    @property
    def fact_check(self) -> dict[str, dict]:
        return dict(self.d.get("fact_check") or {})

    FACT_ICON = {"confirmed": "✅", "refuted": "❌", "unclear": "❔", "user_doc": "📄"}
    FACT_NAME = {"confirmed": "подтверждено", "refuted": "опровергнуто", "unclear": "не удалось проверить",
                 "user_doc": "из документов пользователя"}

    @property
    def fact_counts(self) -> dict[str, int]:
        c = {"confirmed": 0, "refuted": 0, "unclear": 0, "user_doc": 0}
        for r in self.fact_check.values():
            for cl in r.get("claims", []):
                c[cl.get("status") if cl.get("status") in c else "unclear"] += 1
        return c

    @property
    def fact_line(self) -> str:
        """«проверены варианты B, A: ✅ 4 · ❌ 1 · ❔ 2» или пустая строка."""
        if not self.fact_check:
            return ""
        c = self.fact_counts
        return (f"проверены варианты {', '.join(self.fact_check)}: ✅ {c['confirmed']} подтверждено · "
                f"❌ {c['refuted']} опровергнуто · ❔ {c['unclear']} не удалось проверить"
                + (f" · 📄 {c['user_doc']} из документов пользователя" if c["user_doc"] else ""))

    # ------------------------------------------------------------ требования, ничья, решающие голоса, покрытие

    @property
    def requirements(self) -> list[str]:
        return list(self.d.get("requirements") or [])

    @property
    def top_tie(self) -> list[str]:
        """Варианты, разделившие первое место (их различает только жребий)."""
        return list(self.d.get("co_leaders") or [])

    @property
    def tie_line(self) -> str:
        t = self.top_tie
        if len(t) < 2:
            return ""
        w = self.winner
        return (f"Ничья за первое место: {', '.join(t)} — по голосам их не различить"
                + (f"; председатель выбрал {w}" if w in t else ""))

    @property
    def decisive_notes(self) -> list[str]:
        return [f"без бюллетеня {v} лидером был бы вариант {alt}" for v, alt in (self.d.get("decisive") or {}).items()]

    @property
    def coverage_line(self) -> str:
        c = self.d.get("coverage") or {}
        if not c:
            return ""
        if c.get("options", 0) < 3:
            return ("Вариантов меньше трёх — голосование почти ничего не решает: каждая пара сравнивалась "
                    f"{c['min']}–{c['max']} раз, по сути выбор делает председатель")
        rng = f"{c['min']}" if c["min"] == c["max"] else f"от {c['min']} до {c['max']}"
        n = c["max"]
        times = "раза" if c["min"] == c["max"] and n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"
        warn = " — мало данных, относитесь к порядку осторожно" if c["min"] < 2 else ""
        return f"Каждая пара вариантов сравнивалась в бюллетенях {rng} {times}{warn}"

    @staticmethod
    def safe_url(u: str) -> str:
        u = str(u or "").strip()
        return u if u.lower().startswith(("http://", "https://")) else ""

    @property
    def by_schulze(self) -> bool:
        """Заседание посчитано методом Шульце (новые) или по Борда (старые)."""
        return bool(self.tally) and "schulze" in self.tally[0]

    @property
    def order_note(self) -> str:
        from ..voting import ORDER_NOTE
        if self.by_schulze:
            return ORDER_NOTE
        return "Порядок: балл Борда; при равенстве — попарное сравнение, затем оценка критики (как считалось до перехода на метод Шульце)."

    @staticmethod
    def schulze_str(row: dict, total: int) -> str:
        """«3 из 4» — скольких соперников вариант побеждает по Шульце."""
        return "—" if row.get("schulze") is None else f"{row['schulze']} из {max(total - 1, 0)}"

    @property
    def deviation(self) -> dict | None:
        """Председатель назвал победителем не лидера голосования: лидер, победитель, причина."""
        w = self.winner
        if not w or not self.tally:
            return None
        lead = self.tally[0].get("label")
        if not lead or lead == w or w in self.top_tie:
            return None  # лидер или один из разделивших первое место — не отступление
        reason = (self.verdict.get("deviation") or "").strip() or (self.verdict.get("why") or "").strip()
        kind = reason.split(":", 1)[0].strip().lower() if ":" in reason else ""
        names = {"fact": "опровергнутые факты у лидера", "risk": "серьёзный риск у лидера",
                 "process": "лидер не отвечает на вопрос или нарушает требования запроса"}
        text = reason.split(":", 1)[1].strip() if kind in names else reason
        return {"leader": lead, "winner": w, "kind": names.get(kind, ""), "text": text}

    @property
    def deviation_line(self) -> str:
        dv = self.deviation
        if not dv:
            return ""
        head = f"Председатель выбрал не лидера голосования: победитель {dv['winner']}, лидер подсчёта — {dv['leader']}"
        return head + (f". Причина: {dv['kind']}" if dv["kind"] else "") + (f" — {dv['text']}" if dv["text"] else "")

    @staticmethod
    def pairwise_str(row: dict) -> str:
        """Попарный счёт (побед минус поражений) или «—» для старых заседаний."""
        return "—" if row.get("pairwise") is None else f"{float(row['pairwise']):+.0f}"

    @property
    def tie_notes(self) -> list[str]:
        """Пояснения, чем решены ничьи по баллу Борда."""
        from ..voting import TIE_NAME
        rows = self.tally
        basis = "по Шульце" if self.by_schulze else "по баллу Борда"
        return [f"{a.get('label')} выше {b.get('label')}: при равенстве {basis} решило "
                f"{TIE_NAME.get(a['tie_break'], a['tie_break'])}"
                for a, b in zip(rows, rows[1:]) if a.get("tie_break")]

    @property
    def cache_line(self) -> str:
        """«Из кэша провайдеров прочитано 120 000 из 400 000 входных токенов (30%) — они стоят дешевле»."""
        cached = sum(int((s or {}).get("cached_tokens") or 0) for s in self.stats.values())
        total = sum(int((s or {}).get("prompt_tokens") or 0) for s in self.stats.values())
        if not cached or not total:
            return ""
        def n(x: int) -> str:
            return f"{x:,}".replace(",", " ")
        return (f"Из кэша провайдеров прочитано {n(cached)} из {n(total)} входных токенов ({cached / total:.0%}) — "
                "повторно прочитанные вопрос и вложения стоят дешевле")

    @property
    def compact_note(self) -> str:
        """Заседание шло в режиме «Компактные варианты»."""
        if (self.d.get("request") or {}).get("compact_proposals") and not self.is_research:
            return "компактные — участники писали суть сжато, полный формат — только в итоговом решении"
        return ""

    @property
    def chair_note(self) -> str:
        """«Председатель claude — без своего варианта» (только для новых заседаний, где это так)."""
        if self.d.get("chair_proposed", True) or self.d.get("mode") == "evaluate":
            return ""
        chair = (self.d.get("request") or {}).get("chair") or ""
        return f"председатель {chair} — без своего варианта (только постановка, оценка и итог)" if chair else \
            "председатель — без своего варианта (только постановка, оценка и итог)"

    def place(self, label: str) -> int | None:
        for i, r in enumerate(self.tally, 1):
            if r.get("label") == label:
                return i
        return None

    @property
    def ballots(self) -> dict[str, dict]:
        return self.d.get("ballots") or {}

    @property
    def critiques(self) -> dict[str, dict]:
        return self.d.get("critiques") or {}

    def reviews_for(self, label: str) -> list[tuple[str, dict]]:
        out = []
        for reviewer, cs in self.critiques.items():
            for r in cs.get("reviews", []):
                if r.get("label") == label:
                    out.append((reviewer, r))
        return out

    def matrix(self) -> tuple[list[str], list[tuple[str, list[float | None]]], list[float | None]]:
        """(метки, [(рецензент, [средняя оценка по каждой метке])], средние по столбцам)."""
        labels = list(self.proposals)
        rows = []
        cols: dict[str, list[float]] = {l: [] for l in labels}
        for reviewer, cs in self.critiques.items():
            by = {r.get("label"): r for r in cs.get("reviews", [])}
            vals: list[float | None] = []
            for l in labels:
                r = by.get(l)
                if r and r.get("scores"):
                    v = mean(float(x) for x in r["scores"].values())
                    cols[l].append(v)
                    vals.append(v)
                else:
                    vals.append(None)
            rows.append((reviewer, vals))
        return labels, rows, [mean(cols[l]) if cols[l] else None for l in labels]

    def failures(self) -> list[tuple[str, str, str]]:
        out = []
        for name, s in self.stats.items():
            for f in (s or {}).get("failures", []):
                stage, _, err = f.partition(": ")
                out.append((name, STAGE_NAME.get(stage, stage), err))
        chair_fail = chair_failure(self.d)
        if chair_fail and not any(st == STAGE_NAME["verdict"] for _, st, _ in out):
            out.append((chair_fail[0], STAGE_NAME["verdict"], chair_fail[1]))
        return out


def chair_failure(d: dict[str, Any]) -> tuple[str, str] | None:
    """Итог написан без председателя (запасной путь), а сбой не записан — так было до исправления.
    Возвращает (председатель, причина по последней ошибке в стенограмме) или None."""
    vd = d.get("verdict") or {}
    if not str(vd.get("why", "")).startswith("Председатель не ответил"):
        return None
    tr = [e for e in d.get("transcript") or [] if e.get("stage") == "verdict"]
    chair = (tr[-1].get("member") if tr else "") or (d.get("request") or {}).get("chair") or "председатель"
    errs = [e.get("error") for e in tr if e.get("error")]
    reason = f"председатель не сдал итог ({len(tr)} попыт.)" + (f"; последняя причина: {errs[-1][:400]}" if errs else "")
    return chair, reason


# ------------------------------------------------------------------ Markdown → HTML

_LIST = re.compile(r"^(\s*)([-*+•]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s*\|")
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s")


def normalize_md(text: str) -> str:
    """Приводит Markdown от моделей к виду, который понимает Python-Markdown.

    Модели часто пишут список или таблицу сразу после абзаца (без пустой строки)
    и делают вложенность в 2 пробела; маркер «•» — тоже частый гость.
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    indents = [len(m.group(1)) for m in map(_LIST.match, lines) if m and m.group(1)]
    unit = min(indents) if indents else 4  # шаг вложенности, которым пользовалась модель
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            if in_code and out and out[-1].strip():
                out.append("")
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = _LIST.match(line)
        if m:
            level = round(len(m.group(1)) / unit)
            marker = "-" if m.group(2) == "•" else m.group(2)
            line = "    " * level + marker + " " + line[m.end():]
        prev = out[-1] if out else ""
        block_start = bool(m) or _TABLE.match(line) or _HEAD.match(line)
        pm = _LIST.match(prev)
        same_list = bool(pm and m) and not (
            not pm.group(1) and not m.group(1) and pm.group(2)[0].isdigit() != m.group(2)[0].isdigit()
        )  # смена «- …» на «1. …» на верхнем уровне — это новый список
        prev_same = same_list or (_TABLE.match(prev) and _TABLE.match(line))
        if block_start and prev.strip() and not prev_same:
            out.append("")
        if prev.strip() and _HEAD.match(prev) and line.strip():
            out.append("")
        elif pm and not m and line.strip() and not line[:1].isspace() and not _TABLE.match(line):
            out.append("")  # обычный абзац сразу после списка — конец списка, а не продолжение пункта
        out.append(line)
    return "\n".join(out)


_TABLE_HTML = re.compile(r"<table>(.*?)</table>", re.S)


def _wrap_table(m: re.Match) -> str:
    """Оборачивает таблицу и помечает её ширину: широким — мельче шрифт (PDF) и прокрутка (экран)."""
    body = m.group(1)
    first_row = re.search(r"<tr>(.*?)</tr>", body, re.S)
    cols = len(re.findall(r"<t[hd][ >]", first_row.group(1))) if first_row else 0
    size = "xwide" if cols >= 8 else "wide" if cols >= 6 else "normal"
    return f'<div class="tbl"><table class="mdt {size}" data-cols="{cols}">{body}</table></div>'


def short_title(text: str, limit: int = 110) -> str:
    """Первая содержательная строка без Markdown-разметки, не длиннее limit (обрезка по слову)."""
    for raw in str(text or "").splitlines():
        line = re.sub(r"^\s*(#+|[-*+•>]|\d+[.)])\s+", "", raw)
        line = re.sub(r"[*_`]+", "", line).strip()
        if line:
            break
    else:
        return ""
    many_lines = len([x for x in str(text).splitlines() if x.strip()]) > 1
    # длинный запрос: в заголовок — первое предложение, если оно короткое
    m = re.match(r"(.{12,90}?[.!?…])(\s|$)", line)
    if m and (many_lines or len(line) > limit):
        return m.group(1)
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0] if " " in line[:limit] else line[:limit]
    return cut.rstrip(",;:—-– ") + "…"


# ------------------------------------------------------------------ формулы LaTeX → читаемый текст

_GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
          "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
          "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
          "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
          "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω"}
_SYMBOLS = {"times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "approx": "≈", "sim": "∼", "neq": "≠",
            "ne": "≠", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ll": "≪", "gg": "≫", "infty": "∞",
            "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇", "in": "∈", "notin": "∉",
            "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "to": "→",
            "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftrightarrow": "⇔", "mapsto": "↦",
            "ldots": "…", "dots": "…", "cdots": "⋯", "degree": "°", "circ": "∘", "propto": "∝", "equiv": "≡",
            "log": "log", "ln": "ln", "exp": "exp", "min": "min", "max": "max", "lim": "lim", "sin": "sin",
            "cos": "cos", "tan": "tan", "mid": "|", "lvert": "|", "rvert": "|", "langle": "⟨", "rangle": "⟩",
            ",": " ", ";": " ", "quad": "  ", "qquad": "    ", "!": "", "%": "%", "$": "$", "{": "{", "}": "}"}


def _brace(src: str, i: int) -> tuple[str, int]:
    """Аргумент команды: {…} с учётом вложенности или один символ. Возвращает (текст, позиция после)."""
    while i < len(src) and src[i] == " ":
        i += 1
    if i >= len(src):
        return "", i
    if src[i] != "{":
        if src[i] == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", src[i:])
            return m.group(0), i + len(m.group(0))
        return src[i], i + 1
    depth, j = 0, i
    while j < len(src):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[i + 1:j], j + 1
        j += 1
    return src[i + 1:], len(src)


def latex_to_html(src: str) -> str:
    """Упрощённая вёрстка формулы без внешних библиотек: греческие буквы и знаки — символами Unicode,
    степени и индексы — <sup>/<sub>, дроби — a⁄b, корень — √(…). Читается на экране, в PDF и в Word."""
    from html import escape
    out: list[str] = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", src[i:])
            name = m.group(1)
            i += len(m.group(0))
            if name in ("frac", "dfrac", "tfrac"):
                a, i = _brace(src, i)
                b, i = _brace(src, i)
                wrap = lambda x: f"({latex_to_html(x)})" if len(x.strip()) > 1 and not x.strip().isalnum() else latex_to_html(x)  # noqa: E731
                out.append(f"{wrap(a)}⁄{wrap(b)}")
            elif name == "sqrt":
                a, i = _brace(src, i)
                out.append(f"√({latex_to_html(a)})" if len(a) > 1 else f"√{latex_to_html(a)}")
            elif name in ("text", "mathrm", "textrm", "operatorname", "mathbf", "textbf", "mathit", "mbox"):
                a, i = _brace(src, i)
                inner = latex_to_html(a) if name not in ("text", "textrm", "mbox") else escape(a)
                out.append(f"<b>{inner}</b>" if name in ("mathbf", "textbf") else f'<span class="mt">{inner}</span>')
            elif name in ("left", "right", "big", "Big", "bigg", "displaystyle", "limits", "nolimits"):
                pass
            elif name in _GREEK:
                out.append(_GREEK[name])
            elif name in _SYMBOLS:
                out.append(_SYMBOLS[name])
            else:
                out.append(escape(name))
        elif ch in "^_":
            a, i = _brace(src, i + 1)
            tag = "sup" if ch == "^" else "sub"
            out.append(f"<{tag}>{latex_to_html(a)}</{tag}>")
        elif ch in "{}":
            i += 1
        else:
            out.append(escape(ch))
            i += 1
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


_FENCE = re.compile(r"(^```.*?^```[^\n]*$)", re.S | re.M)
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.S)
_MATH_BLOCK = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
_MATH_INLINE = re.compile(r"\\\((.+?)\\\)|(?<![\\$\w])\$(?![\s$])([^$\n]{1,240}?)(?<![\s\\])\$(?![\w$])")
_BARE_URL = re.compile(r"(?<![(<\[=\"'/\w])(https?://[^\s<>()\[\]\"']+[^\s<>()\[\]\"'.,;:!?»)])")


def _looks_like_math(s: str) -> bool:
    """Отличает формулу «$x^2$» от цен «$215 и $300»."""
    return bool(re.search(r"[\\^_=]", s)) or bool(re.fullmatch(r"[A-Za-z]", s.strip()))


def _protect(text: str) -> tuple[str, list[str]]:
    """Формулы и голые ссылки до разбора Markdown: формулы — в метки (чтобы _ и * в них не стали курсивом),
    ссылки — в <…> (автоссылка Markdown). Код (```…``` и `…`) не трогаем."""
    store: list[str] = []

    def keep(html: str, block: bool) -> str:
        store.append(html)
        return f"\n\n⟦M{len(store) - 1}⟧\n\n" if block else f"⟦m{len(store) - 1}⟧"

    def prose(part: str) -> str:
        pieces = _CODE_SPAN.split(part)
        res = []
        k = 0
        while k < len(pieces):
            seg = pieces[k]
            seg = _MATH_BLOCK.sub(lambda m: keep(latex_to_html(m.group(1) or m.group(2)), True), seg)
            seg = _MATH_INLINE.sub(lambda m: keep(latex_to_html(m.group(1) or m.group(2)), False)
                                   if m.group(1) or _looks_like_math(m.group(2)) else m.group(0), seg)
            seg = _BARE_URL.sub(r"<\1>", seg)
            res.append(seg)
            if k + 2 < len(pieces) + 1 and k + 1 < len(pieces):
                res.append(pieces[k + 1] + pieces[k + 2] + pieces[k + 1])  # `код` как был
            k += 3
        return "".join(res)

    parts = _FENCE.split(text)
    return "".join(p if i % 2 else prose(p) for i, p in enumerate(parts)), store


def _restore(html: str, store: list[str]) -> str:
    html = re.sub(r"<p>⟦M(\d+)⟧</p>", lambda m: f'<div class="math-block">{store[int(m.group(1))]}</div>', html)
    html = re.sub(r"⟦M(\d+)⟧", lambda m: f'<span class="math">{store[int(m.group(1))]}</span>', html)
    return re.sub(r"⟦m(\d+)⟧", lambda m: f'<span class="math">{store[int(m.group(1))]}</span>', html)


def md_to_html(text: str, *, breaks: bool = False) -> str:
    """Безопасно: сырой HTML из ответа модели не исполняется, а выводится как текст.

    breaks=True сохраняет одиночные переносы строк (для текста запроса, написанного человеком).
    """
    ext = ["tables", "fenced_code", "sane_lists"] + (["nl2br"] if breaks else [])
    md = _markdown.Markdown(extensions=ext, output_format="html")
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    src, store = _protect(normalize_md(text))
    return _TABLE_HTML.sub(_wrap_table, _restore(md.convert(src), store))


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many
