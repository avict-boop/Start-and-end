# Образ «Совета нейросетей»: один образ для Telegram-бота, веб-интерфейса и командной строки.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Для отчётов в PDF: движок вёрстки (Pango) и шрифты с кириллицей:
# Inter — основной текст отчётов, JetBrains Mono — код, DejaVu — запасной (символы, формулы)
# Распознавание сканов (Tesseract: письменность определяется сама — китайский, японский, корейский, русский,
# европейские, турецкий, вьетнамский, тайский, хинди, арабский, иврит, греческий) и страницы PDF в картинки (poppler-utils):
# сертификаты и годовые отчёты без текстового слоя
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
       fonts-dejavu-core fonts-inter fonts-jetbrains-mono \
       tesseract-ocr tesseract-ocr-osd tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
       tesseract-ocr-jpn tesseract-ocr-kor tesseract-ocr-rus tesseract-ocr-ukr tesseract-ocr-deu tesseract-ocr-fra \
       tesseract-ocr-spa tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-tur tesseract-ocr-pol tesseract-ocr-nld \
       tesseract-ocr-vie tesseract-ocr-tha tesseract-ocr-hin tesseract-ocr-ara tesseract-ocr-heb tesseract-ocr-ell \
       poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Браузер (Chromium) для страниц на JavaScript и сайтов, не отдающих текст простому запросу. Ставится в общую
# папку, чтобы им мог пользоваться непривилегированный пользователь council. Не установился — программа работает
# без него (такие страницы остаются «не открылась»)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN (python -m playwright install --with-deps chromium && chmod -R a+rX /ms-playwright) \
    || echo "Браузер не установился — «трудные» страницы читаться не будут"

COPY council ./council
COPY config.yaml ./config.yaml

# Работаем не от root; папка runs/ — для отчётов и настроек бота (подключается как том)
RUN useradd --create-home --uid 1000 council \
    && mkdir -p /app/runs \
    && chown -R council:council /app/runs
# Контейнер стартует от root лишь на миг: вернуть права на папку данных (после переноса архива она бывает у root)
# и сразу перейти на council — сама программа работает без прав root (deploy/entrypoint.sh)
COPY deploy/entrypoint.sh /usr/local/bin/council-entrypoint
RUN chmod 755 /usr/local/bin/council-entrypoint
ENV HOME=/home/council
ENTRYPOINT ["council-entrypoint"]

# По умолчанию — Telegram-бот. Веб: `python -m council web --host 0.0.0.0`
CMD ["python", "-m", "council", "telegram"]
