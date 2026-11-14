"""Загрузка конфигурации совета из YAML и ключа из окружения / .env."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Mode = Literal["quick", "debate", "evaluate", "research", "creative"]
Effort = Literal["none", "low", "medium", "high"]


class MemberConfig(BaseModel):
    name: str
    model: str
    enabled: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: Effort | None = None  # своя глубина рассуждений для участника
    # Поиск в интернете на этапах, где участник пишет вариант (предложение и доработка): для свежих цифр,
    # цен, новостей. Дороже: OpenRouter берёт плату за результаты поиска (обычно около $0,02 за запрос).
    web_search: bool = False


class BrandingConfig(BaseModel):
    """Фирменное оформление отчётов (необязательно)."""

    title: str = ""   # название компании/проекта в шапке и колонтитулах (по умолчанию «Совет нейросетей»)
    logo: str = ""    # путь к логотипу (PNG/JPG), относительно папки программы, например branding/logo.png
    color: str = ""   # акцентный цвет, например "#0a7d5a"
    footer: str = ""  # доп. текст в колонтитуле, например «Конфиденциально»


class FactCheckConfig(BaseModel):
    """Проверка фактов лидеров голосования моделью с поиском в интернете (необязательно)."""

    enabled: bool = False
    model: str = "perplexity/sonar-pro"   # модель OpenRouter с поиском
    top: int = Field(default=2, ge=1, le=5)          # сколько лидирующих вариантов проверять
    max_claims: int = Field(default=5, ge=1, le=10)  # сколько ключевых утверждений на вариант
    max_tokens: int = 3000
    # Сколько страниц модель проверки может прочитать целиком за один запрос (инструмент OpenRouter web_fetch):
    # видеть страницу, а не фрагмент выдачи, — главное для подтверждения важных утверждений. 0 — не читать
    fetch_pages: int = Field(default=3, ge=0, le=10)
    # Программа сама скачивает страницы, на которые сослались авторы (до lead_pages на утверждение), и передаёт
    # модели проверки отрывки, относящиеся к утверждению. Поиск модели проверки часто не находит сайты компаний
    # на турецком, китайском и других языках (12.11: подтверждено 1 из 31, хотя 22 из 24 ссылок авторов открывались)
    read_leads: bool = True
    lead_pages: int = Field(default=2, ge=0, le=5)


class ResearchConfig(BaseModel):
    """Режим «Исследование»: каждую часть можно выключить, чтобы сравнить на пилоте, что помогает."""

    lenses: bool = True            # у каждого автора свой угол зрения (линза)
    arbitration: bool = True       # арбитраж доказательств после критики: поиск по утверждениям всех вариантов
    missions: int = Field(default=2, ge=1, le=2)        # поисковых вызовов: подтверждения и опровержения
    arbitration_claims: int = Field(default=20, ge=3, le=100)  # сколько утверждений проверять (после объединения дублей)
    max_claims: int = Field(default=8, ge=3, le=80)     # утверждений в варианте (больше — для списков объектов)
    # Обязательный охват объектов: если в задании есть список объектов (компании, продукты, категории), автор, у
    # которого нет утверждений о каком-то из них, получает вариант назад с этим списком (один раз), а на проверку по
    # источникам сначала идут facts_per_object фактов о каждом объекте. В вопросе без списка ничего не делает
    object_coverage: bool = True
    facts_per_object: int = Field(default=1, ge=1, le=5)
    # «Каталог»: число проверок и утверждений у автора считается по числу объектов задания × facts_per_object
    # (не меньше настроек; до 100 проверок и 80 утверждений)
    catalog: bool = False
    # Большой список объектов — председатель сам разбивает задание на части серии (по objects_per_part объектов,
    # по смыслу), части идут с теми же настройками, потом совет сводит их в один отчёт. Выключено — одно заседание
    auto_split: bool = True
    # Сводный отчёт серии: противоречия между частями (одно и то же — разные значения) проверяются поиском;
    # доуточнить неподтверждённое в частях («неясно», «спорно в источниках») — по галочке, дороже
    summary_conflicts: bool = True
    summary_recheck: bool = False
    summary_recheck_max: int = Field(default=20, ge=1, le=60)
    # Сводка серии: варианты пишут столько авторов (председатель + лучшие по частям), критикует и голосует весь совет.
    # Всё уже проверено в частях — пять полных черновиков сводки не нужны. 0 — пишут все
    summary_authors: int = Field(default=2, ge=0, le=10)
    # Кто пишет варианты (пусто — все участники); задаёт программа (сводка серии)
    authors: list[str] = Field(default_factory=list)
    # Поиск председателя на итоге (если у него включён поиск): gaps — только когда в реестре остались невыясненные
    # важные утверждения или объекты без проверенных фактов; always — всегда; never — никогда
    chair_search: Literal["gaps", "always", "never"] = "gaps"
    objects_per_part: int = Field(default=10, ge=3, le=40)
    max_evidence: int = Field(default=20, ge=5, le=40)  # источников в ответе поиска
    revision_digest: bool = True   # доработка по реестру и выжимкам чужих вариантов, а не по их полным текстам
    # Доработка правками: автор присылает только изменённые блоки своего варианта (программа собирает новую версию),
    # а не переписывает его целиком. На 13.11 доработка — треть счёта исследования, почти всё — переписанный текст
    revision_patches: bool = True
    # Поиск по утверждениям — параллельно с критикой: столько долей проверок (факты об объектах, по подвопросам, по
    # авторам — их отбор от критики не зависит) уходит в поиск сразу после вариантов; остальное — после критики
    # (утверждения, на которые указали критики). 0 — как раньше, весь поиск после критики
    early_search_share: float = Field(default=0.7, ge=0.0, le=1.0)
    # Голосование по выжимке вариантов (главный вывод, утверждения, таблицы, риски, что изменено), а не по полным
    # текстам: ранжирование опирается на суть и реестр, полный текст читают критики и председатель
    vote_digest: bool = True
    section_vote: bool = True      # голосование за лучший вариант по каждому подвопросу
    fresh_scouts: int = Field(default=0, ge=0, le=2)    # авторов с поиском уже на этапе предложений («свежие данные»)
    # Поиск у авторов и на доработке. Выключено: на доработке у автора уже есть реестр проверенных источников, а
    # каждый шаг поиска заново отправляет модели весь контекст доработки (чужие варианты, критику, реестр) —
    # на заседании 12.11 автор с поиском прочитал на доработке ~3 млн токенов
    revision_search: bool = False
    max_words: int = Field(default=1400, ge=0, le=5000)  # ориентир длины варианта; 0 — без ограничения длины
    link_check: bool = True        # после арбитража открыть каждую ссылку реестра: мёртвые не подтверждают
    # Слишком длинный вариант (длиннее max_words × long_factor) возвращается автору на сокращение: длинные
    # варианты дорого читать председателю и участникам. Последняя попытка принимается как есть.
    shorten_long: bool = False  # по умолчанию выключено: переписывание ради длины стоило полного нового ответа
    long_factor: float = Field(default=1.5, ge=1.2, le=5.0)
    # Предел ответа автора (предложение и доработка) = max_words × author_tokens_per_word, но не меньше
    # author_min_tokens (думающим моделям нужно место на рассуждения). 0 — общий max_tokens. Личный max_tokens
    # участника в настройках важнее.
    author_tokens_per_word: float = Field(default=3.0, ge=0, le=20)
    author_min_tokens: int = Field(default=6000, ge=1000, le=64000)
    # Подготовка утверждений перед поиском
    dedupe_claims: bool = True     # одинаковые утверждения разных авторов проверяются один раз
    defer_private: bool = True     # непубличное (права, внутренние цены) и «нигде нет» — сразу в «неизвестно», без поиска
    coverage_first: bool = True    # сначала по утверждению на каждый подвопрос, потом по автору, потом самые важные
    # Найденный фрагмент страницы (не сама страница) — не подтверждение: critical — для спорных и быстро
    # меняющихся утверждений, all — для всех, off — не применять
    snippet_rule: Literal["critical", "all", "off"] = "critical"
    recheck_unclear: int = Field(default=5, ge=0, le=15)        # «не удалось проверить» — второй поиск другими запросами
    post_revision_claims: int = Field(default=4, ge=0, le=10)   # новые утверждения после доработки — проверить до N
    vote_ledger: bool = True       # голосующие видят краткий реестр: что подтверждено, что «из памяти»
    decision_card: bool = True     # председатель пишет карточку решения на одну страницу — она первой в итоге
    mark_unconfirmed: bool = True  # пункт «известно» без источника перенесён — пометить это и в тексте итога
    # «Известно» должно назвать проверенные утверждения реестра, на которые опирается (claim_ids); без них — в «неизвестно»
    require_claim_ids: bool = True
    # В формулировке «известно» не должно быть чисел, которых нет в проверенном утверждении (расширение смысла)
    known_numbers_check: bool = True
    # Утверждение, от которого зависит вывод, но помеченное автором «непубличным», — один раз проверить публичным поиском
    check_decisive_private: bool = True
    # Цитата источника: поиск приводит дословную фразу со страницы, проверка ссылок ищет её на скачанной странице;
    # для важного утверждения не нашлась (а страница открылась и текста на ней достаточно) — не засчитывается
    quote_check: bool = True
    # Теневая смысловая сверка: после итога один пакетный запрос без поиска проверяет, следует ли каждый пункт
    # «известно» из его проверенного утверждения или цитаты. Только записывается и показывается — статус не меняет
    # (для пилота: сравнить с ручной проверкой). Модель — semantic_audit_model, пусто — модель председателя
    semantic_audit: bool = True
    semantic_audit_model: str = ""
    # строк таблиц чужого варианта в выжимке для доработки: таблица идёт целиком до 40 строк; 0 — без таблиц
    digest_table_rows: int = Field(default=12, ge=0, le=40)


    @field_validator("max_words")
    @classmethod
    def _words_ok(cls, v: int) -> int:
        if 0 < v < 300:
            raise ValueError("длина варианта: 0 (без ограничения) или от 300 слов")
        return v

class AdvisorConfig(BaseModel):
    """«Подобрать состав»: рекомендация моделей и режима под вопрос."""

    model: str = ""            # модель-советник; пусто — модель председателя (сильная, рассуждает)
    dossier: bool = True       # собирать «досье» моделей поиском в интернете (обзоры, бенчмарки, известные проблемы)
    dossier_days: int = Field(default=21, ge=1, le=180)  # сколько дней досье считается свежим
    max_candidates: int = Field(default=20, ge=6, le=60)  # сколько моделей каталога рассматривать
    question_chars: int = Field(default=30000, ge=2000, le=200000)  # сколько знаков вопроса видит советник
    history_min_sessions: int = Field(default=5, ge=1, le=100)  # ваша история модели — довод, только с N заседаний
    max_arbitration_claims: int = Field(default=100, ge=3, le=100)   # потолок «утверждений на проверку»
    max_words_cap: int = Field(default=2500, ge=300, le=5000)      # потолок длины варианта исследования


class CouncilConfig(BaseModel):
    language: str = "Russian"
    mode: Mode = "debate"
    debate_rounds: int = Field(default=1, ge=0, le=5)
    min_quorum: int = Field(default=2, ge=1)
    max_cost_usd: float = Field(default=2.0, gt=0)
    temperature: float = 0.7
    max_tokens: int = 16000
    max_tokens_cap: int = 32000  # до скольких токенов можно поднять лимит, если ответ обрезан
    reasoning_effort: Effort = "low"  # глубина скрытых рассуждений «думающих» моделей; none — не задавать
    timeout_s: float = 180
    max_retries: int = 3
    json_retries: int = 2
    max_concurrency: int = 5
    # Температура «судейских» этапов (постановка, критика, голосование, итог): меньше случайности в оценках.
    # Авторы (предложение и доработка) пишут с temperature. None — везде temperature.
    judge_temperature: float | None = Field(default=0.3, ge=0, le=2)
    # Предел ответа председателя на итоге (длинный синтез без склеек из продолжений); 0 — общий max_tokens.
    verdict_max_tokens: int = Field(default=20000, ge=0, le=64000)
    # Потолок ответа на служебных этапах (только понижает общий предел и предел участника). OpenRouter на время
    # запроса откладывает на балансе деньги под весь предел: 60 000 на голосовании, где бюллетень ~3 000, лишь
    # раздувал резерв («баланс зарезервирован»). Упёрся в потолок — ответ дописывается продолжением. 0 — без потолка
    # Какого поставщика модели выбирает OpenRouter: throughput — самый быстрый по выдаче (модель та же), price — самый
    # дешёвый, latency — быстрее начинает отвечать, "" — как решит OpenRouter
    provider_sort: Literal["throughput", "price", "latency", ""] = "throughput"
    framing_max_tokens: int = Field(default=24000, ge=0, le=128000)
    critique_max_tokens: int = Field(default=32000, ge=0, le=128000)
    vote_max_tokens: int = Field(default=12000, ge=0, le=128000)
    # Сколько обязательных требований запроса выписывает постановка (длинные ТЗ бывают с 10+ требованиями).
    max_requirements: int = Field(default=20, ge=1, le=40)
    # «Быстрый ответ»: голосующие называют ценные идеи из вариантов не на первом месте — их не теряем
    quick_keep: bool = True
    # «Выбор из вариантов»: председатель может честно сказать «ни один вариант не подходит» (с причиной и цитатой)
    evaluate_allow_none: bool = True
    # Резерв бюджета: новый запрос не уходит, если потраченное + оценка его цены + оценка уже отправленных запросов
    # достигает лимита. Цена — по каталогу OpenRouter: вход запроса, допустимый выход (доля reserve_output_share
    # от предела ответа), поиск. Возможна чуть более ранняя пауза, зато без перерасхода
    budget_reserve: bool = True
    reserve_output_share: float = Field(default=0.5, ge=0.1, le=1.0)
    # Кворум для обсуждения и исследования при 4+ авторах: при двух вариантах выбор по сути делает председатель
    quorum_large: int = Field(default=3, ge=2, le=10)
    # Предел реально занятой памяти процесса, собирающего отчёты (МБ; 0 — без предела). Процесс превысил — сборка
    # останавливается с понятной ошибкой, сайт и бот продолжают работать. Пик каждой сборки сохраняется
    # («Настройки → Заседание»): по нему и стоит подбирать предел. 600 — для сервера на 1 ГБ, 1200 — на 2 ГБ (сайт и бот — ~300)
    reports_memory_mb: int = Field(default=600, ge=0, le=16000)
    # Объём итога: full — в структуре и объёме, которых требует запрос; brief — кратко (до ~500 слов: решение,
    # главные доводы и оговорки). Меняется для каждого вопроса
    answer_size: Literal["full", "brief"] = "full"
    # Эксперимент для пилота: председатель без своего варианта может не участвовать в критике и голосовании
    # (дешевле; меньше оценок каждой пары). Пока данных нет — оба включены
    chair_critiques: bool = True
    chair_votes: bool = True
    # Общий учёт лимита ключа OpenRouter для сайта и бота: перед запросом остаток лимита сверяется с запросами
    # всех идущих заседаний (не только этого) — пауза до того, как ключ откажет посреди этапа
    key_reserve: bool = True
    # Журнал программы (runs/logs/web.log, bot.log): debug — максимально подробно (каждый запрос к модели с
    # токенами, ценой, временем и номером ответа OpenRouter, каждое событие заседания и действие на сайте и в
    # боте), info — главное, warning — только сбои. Размер: log_files файлов по log_file_mb МБ
    log_level: Literal["debug", "info", "warning"] = "debug"
    log_file_mb: int = Field(default=10, ge=1, le=200)
    log_files: int = Field(default=10, ge=2, le=50)
    # Поиск в интернете через серверный инструмент OpenRouter (web_search) вместо устаревшего плагина «web»
    web_tools: bool = True
    # Чем ищет инструмент web_search: exa — поиск OpenRouter с соблюдением лимитов (не больше 3 поисков по 5
    # результатов за запрос); auto — встроенный поиск провайдера, где он есть (OpenAI, Google, Anthropic): лимиты
    # у OpenAI и Google не действуют, вход может вырасти до миллионов токенов
    search_engine: Literal["exa", "auto", "native", "parallel", "perplexity", "firecrawl"] = "exa"
    # Сколько поисков автор может сделать за один запрос и сколько результатов в каждом (Exa; у встроенного поиска
    # OpenAI и Google не действует). Для широкого скрининга (20–30 кандидатов) — больше: 6–10 поисков. Каждый поиск
    # заново отправляет модели весь контекст запроса, поэтому вход растёт примерно пропорционально числу поисков
    search_uses: int = Field(default=3, ge=1, le=10)
    # Без ограничения числа поисков у авторов: модель ищет, сколько сочтёт нужным (широкий скрининг десятков компаний).
    # Только авторы (варианты и доработка); председатель при итоге всё равно ищет не больше search_uses раз: он читает всё заседание, и каждый его поиск
    # стоит дорого. Дольше и дороже: вход растёт с каждым поиском
    search_unlimited: bool = False
    search_results: int = Field(default=5, ge=1, le=10)
    # Ответ строго JSON-объектом у моделей, которые это поддерживают: меньше платных повторов из-за формата
    json_mode: bool = True
    # Глубина скрытых рассуждений на голосовании (ранжирование готовых вариантов): low — дешевле; "" — как у участника
    vote_reasoning: Literal["", "none", "low", "medium", "high"] = "low"
    # Потолок глубины рассуждений на критике и доработке: полная глубина нужна вариантам и итогу, а разбор чужих
    # вариантов и правки по реестру идут по готовому материалу. Только понижает (ниже у участника — так и остаётся);
    # пусто — как у участника. На 13.11 рассуждения критики и доработки — ~300 тыс. токенов ответа за серию из 2 частей
    critique_reasoning: Literal["", "none", "low", "medium", "high"] = "medium"
    revision_reasoning: Literal["", "none", "low", "medium", "high"] = "medium"
    # Пилот: модель для «ответа одной модели с поиском» — контрольная группа к совету; пусто — модель председателя
    pilot_solo_model: str = ""
    chair: str
    # Предлагает ли председатель свой вариант. False — председатель только ставит задачу, критикует,
    # голосует и пишет итог (нет конфликта «автор и судья»). Если без него вариантов меньше кворума — предлагает.
    chair_proposes: bool = False
    # Председатель следует голосованию: победитель — лидер подсчёта; выбрать другой вариант можно только
    # по названной причине (опровергнутые факты у лидера, неприемлемый риск, лидер не отвечает на вопрос).
    chair_follows_vote: bool = True
    # Компактные варианты: участники пишут суть сжато (до compact_words слов), без оформления из запроса;
    # полный ответ в нужном формате пишет только председатель. Заметно дешевле: модели читают короткие тексты.
    compact_proposals: bool = False
    compact_words: int = Field(default=700, ge=150, le=5000)
    devil_advocate: str | None = None
    runs_dir: str = "runs"
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    fact_check: FactCheckConfig = Field(default_factory=FactCheckConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    transcribe_model: str = "google/gemini-2.5-flash"  # модель OpenRouter для расшифровки голосовых
    members: list[MemberConfig]

    @model_validator(mode="after")
    def _check(self) -> "CouncilConfig":
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError("имена участников должны быть уникальны")
        active = {m.name for m in self.active_members}
        if self.chair not in active:
            raise ValueError(f"председатель '{self.chair}' не найден среди включённых участников")
        if self.devil_advocate and self.devil_advocate not in active:
            raise ValueError(f"адвокат дьявола '{self.devil_advocate}' не найден среди включённых участников")
        if len(active) < self.min_quorum:
            raise ValueError(f"включено {len(active)} участников, а кворум {self.min_quorum}")
        return self

    @property
    def active_members(self) -> list[MemberConfig]:
        return [m for m in self.members if m.enabled]

    def member(self, name: str) -> MemberConfig:
        for m in self.members:
            if m.name == name:
                return m
        raise KeyError(name)


def load_config(path: str | Path) -> CouncilConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return CouncilConfig.model_validate(data)


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный парсер .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        elif " #" in value:  # комментарий после значения: KEY=value  # пояснение
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


# какие настройки исследования можно менять для одного заседания (советник и «Параметры» вопроса)
RESEARCH_TUNABLE = ("lenses", "arbitration", "missions", "arbitration_claims", "max_claims", "revision_digest", "revision_patches", "early_search_share", "vote_digest",
                    "section_vote", "fresh_scouts", "revision_search", "max_words", "shorten_long", "long_factor", "link_check",
                    "recheck_unclear", "post_revision_claims", "vote_ledger", "snippet_rule", "digest_table_rows",
                    "object_coverage", "facts_per_object", "catalog", "auto_split", "objects_per_part",
                    "summary_conflicts", "summary_recheck", "summary_recheck_max", "summary_authors", "authors", "chair_search")


class OverrideError(ValueError):
    """Некорректные пользовательские настройки заседания."""


def with_overrides(
    base: CouncilConfig,
    *,
    members: list[str] | None = None,
    chair: str | None = None,
    devil_advocate: str | None = None,  # "" — убрать адвоката дьявола, None — оставить как есть
    budget: float | None = None,
    chair_proposes: bool | None = None,
    fact_check: bool | None = None,
    compact: bool | None = None,
    research_words: int | None = None,
    research_shorten: bool | None = None,
    research: dict[str, Any] | None = None,
    models: dict[str, str] | None = None,
    extra_members: list[dict[str, str]] | None = None,
    web_search: list[str] | None = None,
    answer_size: str | None = None,
) -> CouncilConfig:
    """Возвращает копию конфига с выбранными участниками/ролями/бюджетом, проверяя результат."""
    from pydantic import ValidationError

    data = base.model_dump()
    if answer_size:
        data["answer_size"] = answer_size
    # Состав на одно заседание (например, по «Подобрать состав»): другая модель у участника, новые участники,
    # поиск в интернете у выбранных. Настройки программы при этом не меняются.
    known = {m["name"] for m in data["members"]}
    for x in extra_members or []:
        name, model = str(x.get("name", "")).strip(), str(x.get("model", "")).strip()
        if not re.fullmatch(r"[\w.-]{1,32}", name) or "/" not in model:
            raise OverrideError(f"некорректный участник: {name or '—'} ({model or '—'})")
        if name in known:
            raise OverrideError(f"участник {name} уже есть")
        data["members"].append({"name": name, "model": model, "enabled": False})
        known.add(name)
    for name, model in (models or {}).items():
        if name not in known:
            raise OverrideError(f"неизвестный участник: {name}")
        if "/" not in str(model):
            raise OverrideError(f"некорректная модель у {name}: {model}")
        for m in data["members"]:
            if m["name"] == name:
                m["model"] = str(model).strip()
    if web_search is not None:
        unknown = set(web_search) - known
        if unknown:
            raise OverrideError(f"неизвестные участники: {', '.join(sorted(unknown))}")
        for m in data["members"]:
            m["web_search"] = m["name"] in web_search
    if members is not None:
        unknown = set(members) - {m["name"] for m in data["members"]}
        if unknown:
            raise OverrideError(f"неизвестные участники: {', '.join(sorted(unknown))}")
        for m in data["members"]:
            m["enabled"] = m["name"] in members
    enabled = [m["name"] for m in data["members"] if m["enabled"]]
    if chair:
        data["chair"] = chair
    elif data["chair"] not in enabled and enabled:
        data["chair"] = enabled[0]
    if devil_advocate is not None:
        data["devil_advocate"] = devil_advocate or None
    elif data.get("devil_advocate") not in enabled:
        data["devil_advocate"] = None
    if budget:
        data["max_cost_usd"] = budget
    if chair_proposes is not None:
        data["chair_proposes"] = chair_proposes
    if fact_check is not None:
        data["fact_check"]["enabled"] = fact_check
    if compact is not None:
        data["compact_proposals"] = compact
    if research_words is not None:  # длина варианта исследования для этого заседания; 0 — без ограничения длины
        data["research"]["max_words"] = research_words
    if research_shorten is not None:
        data["research"]["shorten_long"] = research_shorten
    for k, val in (research or {}).items():  # другие настройки исследования на это заседание (по рекомендации)
        if k not in RESEARCH_TUNABLE:
            raise OverrideError(f"эту настройку исследования нельзя менять для заседания: {k}")
        data["research"][k] = val
    try:
        return CouncilConfig.model_validate(data)
    except ValidationError as e:
        raise OverrideError("; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())) from e
