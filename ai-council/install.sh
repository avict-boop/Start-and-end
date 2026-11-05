#!/usr/bin/env bash
# Установка «Совета нейросетей» на сервер одной командой (Ubuntu/Debian):
#
#   curl -fsSL https://raw.githubusercontent.com/avict-boop/Start-and-end/refs/heads/claude/multi-ai-comparison-tool-50nc5z/ai-council/install.sh | sudo bash
#
# Скрипт можно запускать повторно: он обновит код и предложит оставить уже введённые ключи.
# Ключи вводятся только здесь, на сервере, хранятся в файле .env (права 600) и никуда не отправляются,
# кроме проверочных запросов к самим OpenRouter и Telegram.

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/avict-boop/Start-and-end.git}"
BRANCH="${BRANCH:-claude/multi-ai-comparison-tool-50nc5z}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ai-council}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
TELEGRAM_API="${TELEGRAM_API:-https://api.telegram.org}"
OPENROUTER_API="${OPENROUTER_API:-https://openrouter.ai/api/v1}"
ID_WAIT="${ID_WAIT:-300}"         # сколько секунд ждать сообщения боту
ASSUME_YES="${ASSUME_YES:-0}"     # 1 — без вопросов, всё по умолчанию (ключи берутся из переменных окружения)
SKIP_DEPS="${SKIP_DEPS:-0}"       # 1 — не ставить пакеты и Docker (для тестов)
SKIP_BUILD="${SKIP_BUILD:-0}"     # 1 — не пересобирать образ (для тестов)
APP_DIR="$INSTALL_DIR/ai-council"
IMAGE="ai-council:latest"

# ------------------------------------------------------------------ вывод

if [[ -t 2 ]]; then
  C_STEP=$'\033[1;36m' C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_DIM=$'\033[2m' C_END=$'\033[0m'
else
  C_STEP="" C_OK="" C_WARN="" C_ERR="" C_DIM="" C_END=""
fi
STEP_N=0
step() { STEP_N=$((STEP_N + 1)); printf '\n%s[%d] %s%s\n' "$C_STEP" "$STEP_N" "$1" "$C_END" >&2; }
ok()   { printf '%s  ✓ %s%s\n' "$C_OK" "$1" "$C_END" >&2; }
info() { printf '    %s\n' "$1" >&2; }
warn() { printf '%s  ! %s%s\n' "$C_WARN" "$1" "$C_END" >&2; WARNINGS+=("$1"); }
fail() { printf '\n%s  ✗ %s%s\n' "$C_ERR" "$1" "$C_END" >&2; exit 1; }
WARNINGS=()
trap 'fail "Неожиданная ошибка в строке $LINENO. Запустите скрипт ещё раз; если повторится — пришлите вывод."' ERR

# ------------------------------------------------------------------ ввод (работает и при curl | bash)

TTY=""
if [[ "$ASSUME_YES" != 1 ]] && { : </dev/tty; } 2>/dev/null; then TTY=/dev/tty; fi

ask() {  # ask "Вопрос" "по умолчанию" -> ответ в stdout
  local answer=""
  if [[ -n "$TTY" ]]; then read -r -p "  $1 " answer <"$TTY" || true; fi
  printf '%s' "${answer:-${2:-}}"
}
ask_secret() {
  local answer=""
  if [[ -n "$TTY" ]]; then read -r -s -p "  $1 " answer <"$TTY" || true; printf '\n' >&2; fi
  printf '%s' "$answer" | tr -d '[:space:]'
}
confirm() {  # confirm "Вопрос?" Y|N
  local def="${2:-Y}" hint answer
  [[ "$def" == Y ]] && hint="[Д/н]" || hint="[д/Н]"
  [[ -z "$TTY" ]] && { [[ "$def" == Y ]]; return; }
  answer="$(ask "$1 $hint" "")"
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$answer" ]] && { [[ "$def" == Y ]]; return; }
  [[ "$answer" =~ ^(y|yes|д|да)$ ]]
}
mask() { local s="$1"; ((${#s} > 10)) && printf '%s…%s' "${s:0:6}" "${s: -4}" || printf '***'; }

# ------------------------------------------------------------------ .env

env_file() { printf '%s/.env' "$APP_DIR"; }
env_get() { [[ -f "$(env_file)" ]] && grep -E "^$1=" "$(env_file)" | tail -n1 | cut -d= -f2- | tr -d "\"'" || true; }
env_set() {
  local f tmp; f="$(env_file)"; tmp="$(mktemp)"
  touch "$f"
  awk -v k="$1" -v v="$2" 'BEGIN{d=0} index($0, k"=")==1 {print k"="v; d=1; next} {print} END{if(!d) print k"="v}' "$f" >"$tmp"
  cat "$tmp" >"$f"; rm -f "$tmp"; chmod 600 "$f"
}

# ------------------------------------------------------------------ HTTP-помощники

TG_TOKEN=""
tg() {  # tg метод '{"json":...}' [таймаут] -> тело ответа; код возврата ≠0 при сетевой ошибке
  local data="${2:-}"; [[ -n "$data" ]] || data='{}'
  curl -sS --max-time "${3:-40}" -H 'Content-Type: application/json' -d "$data" \
    "$TELEGRAM_API/bot$TG_TOKEN/$1" 2>/dev/null
}
tg_send() {  # tg_send chat_id "текст"
  local body; body="$(jq -n --arg c "$1" --arg t "$2" '{chat_id: ($c|tonumber), text: $t}')"
  tg sendMessage "$body" >/dev/null || true
}
dc() { (cd "$APP_DIR" && docker compose "$@"); }

# ------------------------------------------------------------------ шаги

check_system() {
  step "Проверка системы"
  [[ $EUID -eq 0 ]] || fail "Нужны права root. Запустите так: curl -fsSL <адрес скрипта> | sudo bash"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release; ok "Система: ${PRETTY_NAME:-неизвестно}"
  fi
  local mem; mem="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  ((mem == 0 || mem >= 700)) || warn "Памяти ${mem} МБ — может не хватить для сборки образа (лучше от 1 ГБ)"
}

install_deps() {
  step "Установка Git и Docker"
  if [[ "$SKIP_DEPS" == 1 ]]; then ok "Пропущено (SKIP_DEPS=1)"; return; fi
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git curl jq ca-certificates >/dev/null
  fi
  for cmd in git curl jq; do command -v "$cmd" >/dev/null || fail "Не найдена программа $cmd. Установите её и запустите скрипт снова."; done
  if ! command -v docker >/dev/null; then
    info "Устанавливаю Docker (1–2 минуты)…"
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || fail "Не удалось установить Docker. Инструкция: https://docs.docker.com/engine/install/"
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || fail "Не найден docker compose. Обновите Docker: curl -fsSL https://get.docker.com | sh"
  ok "Git, jq и $(docker --version | cut -d, -f1) готовы"
}

fetch_code() {
  step "Загрузка программы"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    if git -C "$INSTALL_DIR" fetch -q origin "$BRANCH" && git -C "$INSTALL_DIR" checkout -q "$BRANCH" \
       && git -C "$INSTALL_DIR" merge -q --ff-only --autostash "origin/$BRANCH" 2>/dev/null; then
      ok "Код обновлён в $INSTALL_DIR"
    else
      warn "Не удалось обновить код (возможно, вы меняли файлы вручную) — продолжаю с текущей версией"
    fi
  else
    git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || fail "Не удалось скачать $REPO_URL"
    ok "Код скачан в $INSTALL_DIR"
  fi
  [[ -f "$APP_DIR/docker-compose.yml" ]] || fail "В $APP_DIR нет docker-compose.yml — проверьте ветку $BRANCH"
  [[ -f "$(env_file)" ]] || { cp "$APP_DIR/.env.example" "$(env_file)" 2>/dev/null || touch "$(env_file)"; chmod 600 "$(env_file)"; }
}

or_key_status() {  # HTTP-код проверки ключа OpenRouter
  curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H "Authorization: Bearer $1" "$OPENROUTER_API/$2" 2>/dev/null || echo 000
}

setup_openrouter() {
  step "Ключ OpenRouter"
  local key existing code body
  existing="$(env_get OPENROUTER_API_KEY)"
  [[ "$existing" == "sk-or-v1-..." ]] && existing=""
  key="${OPENROUTER_API_KEY:-}"
  if [[ -z "$key" && -n "$existing" ]] && confirm "Оставить сохранённый ключ $(mask "$existing")?" Y; then key="$existing"; fi
  local tries=0
  while :; do
    if [[ -z "$key" ]]; then
      info "Ключ создаётся на https://openrouter.ai/keys (на счету должно быть немного денег)."
      key="$(ask_secret "Вставьте ключ OpenRouter (ввод скрыт; Enter — пропустить, будет только демо-режим):")"
    fi
    if [[ -z "$key" ]]; then
      warn "Ключ OpenRouter не задан — бот будет работать только в демо-режиме. Добавить позже: ai-council env"
      env_set OPENROUTER_API_KEY ""; return
    fi
    code="$(or_key_status "$key" key)"
    [[ "$code" == 404 ]] && code="$(or_key_status "$key" auth/key)"   # старый адрес того же API
    if [[ "$code" == 200 ]]; then
      ok "Ключ принят"
      break
    elif [[ "$code" == 401 || "$code" == 403 ]]; then
      tries=$((tries + 1))
      ((tries >= 3)) && fail "OpenRouter не принимает ключ. Проверьте его на https://openrouter.ai/keys"
      warn "OpenRouter не принял ключ (HTTP $code). Попробуйте ещё раз."
      key=""; [[ -z "$TTY" ]] && fail "Неверный ключ OpenRouter"
    else
      warn "Не удалось проверить ключ (ответ: $code) — сохраняю как есть. Если OpenRouter недоступен с этого сервера, модели работать не будут."
      break
    fi
  done
  env_set OPENROUTER_API_KEY "$key"
  body="$(curl -sS --max-time 20 -H "Authorization: Bearer $key" "$OPENROUTER_API/credits" 2>/dev/null || true)"
  local left
  left="$(printf '%s' "$body" | jq -r 'try ((.data.total_credits // empty) - (.data.total_usage // 0)) catch empty' 2>/dev/null || true)"
  if [[ -n "$left" ]]; then
    if awk -v x="$left" 'BEGIN{exit !(x <= 0.01)}'; then
      warn "Баланс OpenRouter почти нулевой (\$$left) — пополните его: https://openrouter.ai/credits"
    else
      ok "Баланс OpenRouter: \$$(printf '%.2f' "$left")"
    fi
  fi
}

setup_telegram() {
  step "Telegram-бот"
  local token existing resp
  existing="$(env_get TELEGRAM_BOT_TOKEN)"
  token="${TELEGRAM_BOT_TOKEN:-}"
  if [[ -z "$token" && -n "$existing" ]] && confirm "Оставить сохранённый токен бота $(mask "$existing")?" Y; then token="$existing"; fi
  local tries=0
  while :; do
    if [[ -z "$token" ]]; then
      info "Токен выдаёт @BotFather в Telegram: команда /newbot."
      token="$(ask_secret "Вставьте токен бота (ввод скрыт):")"
      [[ -n "$token" ]] || fail "Без токена бот работать не может. Получите его у @BotFather и запустите скрипт снова."
    fi
    TG_TOKEN="$token"
    if ! resp="$(tg getMe '{}' 20)" || [[ -z "$resp" ]]; then
      fail "Сервер не может связаться с api.telegram.org. Вероятно, Telegram заблокирован в сети провайдера — возьмите сервер в другой стране."
    fi
    if [[ "$(printf '%s' "$resp" | jq -r '.ok')" == true ]]; then
      BOT_USERNAME="$(printf '%s' "$resp" | jq -r '.result.username')"
      ok "Бот найден: @$BOT_USERNAME"
      break
    fi
    tries=$((tries + 1))
    ((tries >= 3)) && fail "Telegram не принимает токен. Проверьте его у @BotFather (/mybots → API Token)."
    warn "Telegram не принял токен: $(printf '%s' "$resp" | jq -r '.description // "ошибка"'). Попробуйте ещё раз."
    token=""; [[ -z "$TTY" ]] && fail "Неверный токен бота"
  done
  env_set TELEGRAM_BOT_TOKEN "$token"
  local hook
  hook="$(tg getWebhookInfo '{}' 20 | jq -r '.result.url // ""' 2>/dev/null || true)"
  if [[ -n "$hook" ]]; then
    warn "У бота настроен webhook ($hook) — с ним бот не получает сообщения этим способом"
    if confirm "Отключить webhook?" Y; then tg deleteWebhook '{}' >/dev/null && ok "Webhook отключён"; fi
  fi
}

detect_user() {
  step "Доступ к боту"
  local allowed existing
  existing="$(env_get TELEGRAM_ALLOWED_USERS)"
  allowed="${TELEGRAM_ALLOWED_USERS:-}"
  if [[ -z "$allowed" && -n "$existing" ]] && confirm "Оставить доступ для ID: $existing?" Y; then allowed="$existing"; fi
  if [[ -z "$allowed" ]]; then
    dc stop bot >/dev/null 2>&1 || true   # пока бот запущен, он сам забирает сообщения
    local resp offset="" deadline found=""
    resp="$(tg getUpdates '{"timeout":0}' 20 || true)"
    if [[ "$(printf '%s' "$resp" | jq -r '.error_code // 0' 2>/dev/null)" == 409 ]]; then
      fail "Этот бот уже запущен где-то ещё (например, на вашем компьютере). Остановите его там и запустите скрипт снова."
    fi
    offset="$(printf '%s' "$resp" | jq -r '[.result[]?.update_id] | max // empty | . + 1' 2>/dev/null || true)"
    printf '\n%s  ➜ Откройте Telegram и напишите боту @%s любое сообщение, например «привет».%s\n' "$C_STEP" "$BOT_USERNAME" "$C_END" >&2
    if ((ID_WAIT >= 60)); then info "Жду до $((ID_WAIT / 60)) мин…"; else info "Жду до $ID_WAIT с…"; fi
    deadline=$((SECONDS + ID_WAIT))
    while ((SECONDS < deadline)); do
      resp="$(tg getUpdates "{\"timeout\":25${offset:+,\"offset\":$offset}}" 35 || true)"
      [[ -z "$resp" ]] && { sleep 3; continue; }
      if [[ "$(printf '%s' "$resp" | jq -r '.error_code // 0')" == 409 ]]; then
        fail "Этот бот уже запущен где-то ещё. Остановите другой экземпляр и запустите скрипт снова."
      fi
      local last; last="$(printf '%s' "$resp" | jq -r '[.result[]?.update_id] | max // empty')"
      [[ -n "$last" ]] && offset=$((last + 1))
      found="$(printf '%s' "$resp" | jq -r '[.result[]? | (.message // .edited_message // empty) | select(.chat.type == "private") | .from]
                 | first // empty | "\(.id)\t\(.first_name // "")\t\(.username // "")"')"
      [[ -n "$found" ]] && break
    done
    if [[ -n "$found" ]]; then
      local uid name uname; IFS=$'\t' read -r uid name uname <<<"$found"
      if confirm "Сообщение от: ${name}${uname:+ (@$uname)}, ID $uid. Это вы?" Y; then
        allowed="$uid"
        tg getUpdates "{\"timeout\":0,\"offset\":$offset}" 20 >/dev/null || true   # пометить сообщение прочитанным
        tg_send "$uid" "✅ Доступ открыт. Ещё минута — и совет будет готов к работе."
      fi
    else
      warn "Сообщение не пришло за отведённое время"
    fi
    while [[ ! "$allowed" =~ ^-?[0-9]+(,-?[0-9]+)*$ ]]; do
      [[ -z "$TTY" ]] && fail "Не удалось определить ваш Telegram ID. Задайте его: TELEGRAM_ALLOWED_USERS=<id>"
      info "Свой ID можно узнать у бота @userinfobot."
      allowed="$(ask "Введите ваш Telegram ID (цифры; несколько — через запятую):" "" | tr -d '[:space:]')"
    done
  fi
  env_set TELEGRAM_ALLOWED_USERS "$allowed"
  ALLOWED="$allowed"
  ok "Пользоваться ботом можно с ID: $allowed"
}

build_image() {
  step "Сборка программы"
  if [[ "$SKIP_BUILD" == 1 ]]; then ok "Пропущено (SKIP_BUILD=1)"; return; fi
  info "Это займёт 1–3 минуты при первом запуске…"
  dc build -q bot >/tmp/ai-council-build.log 2>&1 || { tail -n 20 /tmp/ai-council-build.log >&2; fail "Сборка не удалась (подробности выше)"; }
  rm -f /tmp/ai-council-build.log
  ok "Образ собран"
}

# Правка config.yaml с сохранением комментариев: replace <участник> <модель> | disable <участник> | top <ключ> <значение>
edit_config() {
  docker run --rm -i --user 0 --entrypoint python -v "$APP_DIR/config.yaml:/cfg.yaml" "$IMAGE" - "$@" <<'PY'
import re, sys
action, *args = sys.argv[1:]
path = "/cfg.yaml"
lines = open(path, encoding="utf-8").read().split("\n")
if action == "top":
    key, value = args
    for i, l in enumerate(lines):
        if re.match(rf"{key}\s*:", l):
            lines[i] = f"{key}: {value}"
            break
    else:
        lines.insert(0, f"{key}: {value}")
else:
    name = args[0]
    start = next(i for i, l in enumerate(lines) if re.match(rf"\s*-\s*name:\s*{re.escape(name)}\s*(#.*)?$", l))
    end = next((j for j in range(start + 1, len(lines)) if re.match(r"\s*-\s*name:", lines[j])
                or (lines[j].strip() and not lines[j].startswith(" ") and not lines[j].startswith("#"))), len(lines))
    block = range(start + 1, end)
    model_i = next(i for i in block if re.match(r"\s+model\s*:", lines[i]))
    indent = re.match(r"\s*", lines[model_i]).group(0)
    if action == "replace":
        lines[model_i] = f"{indent}model: {args[1]}"
    elif action == "disable":
        en = next((i for i in block if re.match(r"\s+enabled\s*:", lines[i])), None)
        if en is None:
            lines.insert(model_i + 1, f"{indent}enabled: false")
        else:
            lines[en] = f"{indent}enabled: false"
with open(path, "w", encoding="utf-8") as f:   # запись поверх, чтобы не сломать монтирование файла
    f.write("\n".join(lines))
PY
}

config_check() {  # печатает «ok» или текст ошибки конфигурации
  docker run --rm -i --entrypoint python -v "$APP_DIR/config.yaml:/app/config.yaml:ro" "$IMAGE" - <<'PY'
from council.config import load_config
try:
    load_config("/app/config.yaml"); print("ok")
except Exception as e:
    print(str(e).replace("\n", " ")[:400])
PY
}

check_models() {
  step "Проверка моделей на OpenRouter"
  local models missing name model sugg
  models="$(curl -sS --max-time 30 "$OPENROUTER_API/models" 2>/dev/null || true)"
  if [[ -z "$models" ]] || ! printf '%s' "$models" | jq -e '.data | length > 0' >/dev/null 2>&1; then
    warn "Не удалось получить список моделей OpenRouter — проверка пропущена"
    return
  fi
  missing="$(printf '%s' "$models" | docker run --rm -i --entrypoint python \
      -v "$APP_DIR/config.yaml:/app/config.yaml:ro" "$IMAGE" -c '
import difflib, json, sys
from council.config import load_config
known = sorted(m["id"] for m in json.load(sys.stdin)["data"])
for m in load_config("/app/config.yaml").active_members:
    if m.model not in known:
        s = difflib.get_close_matches(m.model, known, n=1, cutoff=0.5) or [""]
        print(m.name, m.model, s[0], sep="\t")
')"
  if [[ -z "$missing" ]]; then
    ok "Все модели участников есть на OpenRouter"
  else
    while IFS=$'\t' read -r name model sugg; do
      [[ -z "$name" ]] && continue
      if [[ -n "$sugg" ]] && confirm "Модели «$model» ($name) нет на OpenRouter. Заменить на «$sugg»?" Y; then
        edit_config replace "$name" "$sugg" && ok "$name: $model → $sugg"
      else
        edit_config disable "$name" && warn "$name отключён: модели «$model» нет на OpenRouter (включить: ai-council config)"
      fi
    done <<<"$missing"
  fi
  local res first
  res="$(config_check)"
  if [[ "$res" == *председатель* ]]; then
    first="$(docker run --rm -i --entrypoint python -v "$APP_DIR/config.yaml:/app/config.yaml:ro" "$IMAGE" -c '
import yaml; d = yaml.safe_load(open("/app/config.yaml"))
print(next(m["name"] for m in d["members"] if m.get("enabled", True)))')"
    edit_config top chair "$first" && warn "Председатель заменён на $first"
    res="$(config_check)"
  fi
  if [[ "$res" == *"адвокат"* ]]; then
    edit_config top devil_advocate null && warn "Адвокат дьявола отключён"
    res="$(config_check)"
  fi
  [[ "$res" == ok ]] || fail "Ошибка в config.yaml: $res. Исправьте: nano $APP_DIR/config.yaml"
}

start_bot() {
  step "Запуск бота"
  dc up -d --force-recreate bot >/dev/null 2>&1 || fail "Не удалось запустить контейнер: cd $APP_DIR && docker compose up bot"
  local deadline=$((SECONDS + 60)) logs=""
  while ((SECONDS < deadline)); do
    logs="$(dc logs --no-color bot 2>&1 || true)"
    if grep -q "бот @.* запущен" <<<"$logs"; then ok "Бот @$BOT_USERNAME работает"; return; fi
    if grep -q "Неверный TELEGRAM_BOT_TOKEN" <<<"$logs"; then fail "Бот не принял токен"; fi
    sleep 2
  done
  printf '%s\n' "$logs" | tail -n 15 >&2
  if grep -q "Telegram недоступен" <<<"$logs"; then
    fail "Бот не может подключиться к Telegram с этого сервера (выше логи). Обычно это сетевые ограничения провайдера."
  fi
  fail "Бот не запустился за минуту (выше логи)"
}

offer_web() {
  step "Веб-интерфейс (по желанию)"
  if confirm "Запустить также веб-интерфейс? Он будет доступен только через SSH-туннель." N; then
    if dc --profile web up -d web >/dev/null 2>&1; then ok "Веб-интерфейс запущен"; else warn "Веб-интерфейс не запустился: ai-council logs web"; fi
    WEB=1
  else
    ok "Пропущено (запустить позже: ai-council web)"
  fi
}

install_helper() {
  mkdir -p "$BIN_DIR"
  cat >"$BIN_DIR/ai-council" <<EOF
#!/usr/bin/env bash
# Управление «Советом нейросетей». Установлено скриптом install.sh.
set -e
cd "$APP_DIR"
web_running() { docker compose ps --services --status running 2>/dev/null | grep -qx web; }
case "\${1:-help}" in
  status)  docker compose --profile web ps ;;
  logs)    docker compose logs -f --tail 100 "\${2:-bot}" ;;
  restart) docker compose up -d --force-recreate bot; web_running && docker compose --profile web up -d --force-recreate web || true ;;
  stop)    docker compose --profile web down ;;
  start)   docker compose up -d bot ;;
  update)  git -C "$INSTALL_DIR" pull -q --ff-only --autostash && docker compose build -q && \$0 restart ;;  # правки config.yaml сохраняются
  ask)     shift; docker compose run --rm bot python -m council ask "\$@" ;;
  config)  "\${EDITOR:-nano}" config.yaml && echo "Сохранено: изменения подхватятся со следующего заседания." ;;
  env)     "\${EDITOR:-nano}" .env && docker compose up -d --force-recreate bot ;;
  web)     docker compose --profile web up -d web
           echo "Туннель с вашего компьютера: ssh -L 8000:127.0.0.1:8000 root@<IP сервера>, затем http://localhost:8000" ;;
  backup)  d="runs-backup-\$(date +%F)"; docker compose cp bot:/app/runs "\$d" && echo "Отчёты скопированы в $APP_DIR/\$d" ;;
  *) cat <<'HELP'
ai-council — управление «Советом нейросетей»
  status    состояние            logs [bot|web]  логи
  restart   перезапуск           stop / start    остановить / запустить
  update    обновить программу   ask "вопрос"    заседание из консоли
  config    участники и модели   env             ключи и доступ (.env)
  web       веб-интерфейс        backup          копия отчётов
HELP
  ;;
esac
EOF
  chmod +x "$BIN_DIR/ai-council"
}

summary() {
  local ip
  ip="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [[ -n "${ALLOWED:-}" ]] && tg_send "${ALLOWED%%,*}" "🏛 Совет нейросетей запущен на сервере и готов к работе.
Напишите любой вопрос — я покажу настройки и оценку стоимости. Справка: /help"
  printf '\n%s══════════════════════════════════════════════════%s\n' "$C_OK" "$C_END" >&2
  printf '%s ✅ Готово! Совет нейросетей работает.%s\n' "$C_OK" "$C_END" >&2
  printf '%s══════════════════════════════════════════════════%s\n\n' "$C_OK" "$C_END" >&2
  info "Telegram: напишите вопрос боту @$BOT_USERNAME (он уже прислал приветствие)."
  if [[ "${WEB:-0}" == 1 ]]; then
    info "Веб: на своём компьютере выполните  ssh -L 8000:127.0.0.1:8000 root@${ip:-<IP>}"
    info "     и откройте http://localhost:8000"
  fi
  info ""
  info "Управление: команда ${C_STEP}ai-council${C_END} (status, logs, restart, update, config, env, backup)"
  info "Бот сам перезапускается после сбоев и перезагрузки сервера."
  if ((${#WARNINGS[@]})); then
    printf '\n%s  Обратите внимание:%s\n' "$C_WARN" "$C_END" >&2
    for w in "${WARNINGS[@]}"; do printf '%s  • %s%s\n' "$C_WARN" "$w" "$C_END" >&2; done
  fi
  printf '\n' >&2
}

main() {
  printf '%s\n  Установка «Совета нейросетей»%s\n%s  Код: %s (%s)%s\n' "$C_STEP" "$C_END" "$C_DIM" "$REPO_URL" "$BRANCH" "$C_END" >&2
  check_system
  install_deps
  fetch_code
  setup_openrouter
  setup_telegram
  build_image
  check_models
  detect_user
  start_bot
  offer_web
  install_helper
  summary
}

# Вся логика — в функциях, а вызов в самом конце: bash успеет прочитать скрипт целиком даже при curl | bash.
if [[ "${AI_COUNCIL_SOURCE_ONLY:-0}" != 1 ]]; then main "$@"; fi
