# Образ «Совета нейросетей»: один образ для Telegram-бота, веб-интерфейса и командной строки.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY council ./council
COPY config.yaml ./config.yaml

# Работаем не от root; папка runs/ — для отчётов и настроек бота (подключается как том)
RUN useradd --create-home --uid 1000 council \
    && mkdir -p /app/runs \
    && chown -R council:council /app/runs
USER council

# По умолчанию — Telegram-бот. Веб: `python -m council web --host 0.0.0.0`
CMD ["python", "-m", "council", "telegram"]
