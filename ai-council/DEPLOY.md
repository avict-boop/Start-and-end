# Запуск на сервере (круглосуточно)

После настройки бот работает на сервере постоянно и сам перезапускается после сбоев и перезагрузок.
Вы пишете ему из Telegram с телефона, откуда угодно. Займёт около 15 минут.

## 1. Арендовать сервер

Подойдёт самый простой VPS:

- **Система:** Ubuntu 24.04 (или 22.04);
- **Ресурсы:** 1 vCPU, 1 ГБ RAM, 10 ГБ диска — бот почти ничего не потребляет, всю работу делают модели на стороне OpenRouter;
- **Цена:** примерно $4–6 в месяц (Hetzner, DigitalOcean, Timeweb, Selectel, Aeza и т. п.).

Лучше выбрать сервер **за пределами России**, например в Европе. Часть AI-сервисов ограничивает доступ из
некоторых регионов, а Telegram API бывает недоступен из российских сетей. На сервере в Европе таких
проблем обычно нет.

После оплаты провайдер даст **IP-адрес** сервера и **пароль root** (или предложит добавить SSH-ключ).

## 2. Подключиться к серверу

- **Windows 10/11:** откройте PowerShell; **Mac/Linux:** откройте Терминал. Выполните:
  ```bash
  ssh root@ВАШ_IP
  ```
  При первом подключении ответьте `yes` и введите пароль (символы при вводе не отображаются — это нормально).
- **С телефона:** приложение **Termius** (iOS/Android): New Host → IP, пользователь `root`, пароль.

Все команды ниже выполняются **на сервере**.

## 3. Установить Docker и Git

```bash
apt update && apt install -y git
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

Брандмауэр (рекомендуется): пропускаем только SSH.

```bash
ufw allow OpenSSH && ufw --force enable
```

## 4. Скачать проект

```bash
git clone -b claude/multi-ai-comparison-tool-50nc5z https://github.com/avict-boop/Start-and-end.git
cd Start-and-end/ai-council
```

> Если репозиторий приватный, git спросит логин и пароль. Вместо пароля нужен токен GitHub:
> github.com → Settings → Developer settings → Personal access tokens → Fine-grained token,
> доступ на чтение (Contents: Read) к этому репозиторию.

## 5. Ключи

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Заполните:

```
OPENROUTER_API_KEY=sk-or-v1-...      # https://openrouter.ai/keys
TELEGRAM_BOT_TOKEN=123456:ABC...     # от @BotFather (/newbot)
TELEGRAM_ALLOWED_USERS=              # пока оставьте пустым
```

Сохранить и выйти из nano: `Ctrl+O`, `Enter`, `Ctrl+X`.

Совет: в настройках ключа на OpenRouter можно задать лимит расходов. Тогда даже при ошибке
не потратится больше этой суммы.

## 6. Собрать и проверить

```bash
docker compose build
docker compose run --rm bot python -m council ask "тест" --dry-run
```

Последняя команда сверяет названия моделей с OpenRouter и показывает оценку стоимости.
Если какой-то модели нет, найдите точное название и исправьте `config.yaml`:

```bash
docker compose run --rm bot python -m council models kimi
nano config.yaml
```

## 7. Запустить бота

```bash
docker compose up -d
docker compose logs -f bot        # выход из просмотра логов — Ctrl+C (бот продолжит работать)
```

В логах должно появиться `бот @имя_бота запущен`.

**Открыть себе доступ.** Напишите боту в Telegram — он ответит «Доступ закрыт» и покажет ваш ID.
Впишите его в `.env` (несколько ID — через запятую) и пересоздайте контейнер:

```bash
nano .env                          # TELEGRAM_ALLOWED_USERS=123456789
docker compose up -d --force-recreate bot
```

Готово. Напишите боту вопрос, и совет начнёт заседание.

## 8. Веб-интерфейс на сервере (по желанию)

```bash
docker compose --profile web up -d
```

У веб-интерфейса нет пароля, поэтому он открыт **только внутри сервера**. Снаружи заходите через SSH-туннель:

- **С компьютера:**
  ```bash
  ssh -L 8000:127.0.0.1:8000 root@ВАШ_IP
  ```
  Пока это окно открыто, интерфейс доступен в браузере по адресу http://localhost:8000.
- **С телефона (Termius):** Port Forwarding → New → Local, порт `8000`, хост `127.0.0.1`, порт `8000`,
  выбрать ваш сервер. Затем в браузере телефона откройте http://localhost:8000.

Бот и веб-интерфейс используют общую историю заседаний.

## Повседневные команды

Выполнять в папке `~/Start-and-end/ai-council`:

| Что сделать | Команда |
|---|---|
| Состояние | `docker compose ps` |
| Логи бота | `docker compose logs -f bot` |
| Перезапустить | `docker compose restart` |
| Остановить всё | `docker compose --profile web down` (история сохранится) |
| Запустить снова | `docker compose up -d` |
| Поменять участников/модели | `nano config.yaml`; изменения подхватятся со следующего заседания (если нет — `docker compose restart`) |
| Поменять ключи в `.env` | `nano .env` и `docker compose up -d --force-recreate` |
| Обновить программу | `git pull && docker compose up -d --build` |
| Задать вопрос из консоли | `docker compose run --rm bot python -m council ask "вопрос"` |

**Резервная копия отчётов.** Отчёты и так приходят в Telegram файлами. Чтобы забрать весь архив:

```bash
docker compose cp bot:/app/runs ./runs-backup                  # на сервере
scp -r root@ВАШ_IP:Start-and-end/ai-council/runs-backup .      # на своём компьютере
```

## Если что-то не так

| Симптом | Причина и решение |
|---|---|
| В логах `Неверный TELEGRAM_BOT_TOKEN` | Токен скопирован с ошибкой. Проверьте его у @BotFather и пересоздайте контейнер (`--force-recreate`) |
| В логах повторяется `Telegram недоступен` | Сервер не достаёт до api.telegram.org. Обычно это ограничение сети провайдера: выберите сервер в другой стране |
| Бот отвечает «Доступ закрыт» | Вашего ID нет в `TELEGRAM_ALLOWED_USERS`, или после правки `.env` не пересоздан контейнер |
| Модели не отвечают, ошибки 401/402 | Неверный ключ OpenRouter или закончился баланс |
| Ошибки 403 от моделей | Ограничение по региону: попробуйте другие модели или сервер в другой стране |
| `Cannot connect to the Docker daemon` | Docker не запущен: `systemctl start docker` |
