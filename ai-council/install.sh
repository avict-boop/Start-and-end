#!/usr/bin/env bash
# Установка «Совета нейросетей» на сервер одной командой (Ubuntu/Debian):
#
#   curl -fsSL https://raw.githubusercontent.com/avict-boop/Start-and-end/refs/heads/claude/multi-ai-comparison-tool-50nc5z/ai-council/install.sh | sudo bash
#
# Скрипт спрашивает всё, что нужно для полноценной работы, и для каждого пункта показывает,
# где это взять. Запускать повторно можно: он обновит программу и предложит оставить введённое.
# Ключи и пароли хранятся только на этом сервере: в .env (права 600) и в файле доступа для root.

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/avict-boop/Start-and-end.git}"
BRANCH="${BRANCH:-claude/multi-ai-comparison-tool-50nc5z}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ai-council}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
ACCESS_FILE="${ACCESS_FILE:-/root/ai-council-access.txt}"
TELEGRAM_API="${TELEGRAM_API:-https://api.telegram.org}"
OPENROUTER_API="${OPENROUTER_API:-https://openrouter.ai/api/v1}"
INSTALL_URL="https://raw.githubusercontent.com/avict-boop/Start-and-end/refs/heads/$BRANCH/ai-council/install.sh"
ID_WAIT="${ID_WAIT:-300}"             # сколько секунд ждать сообщения боту
HTTPS_WAIT="${HTTPS_WAIT:-180}"       # сколько ждать выпуска HTTPS-сертификата
ASSUME_YES="${ASSUME_YES:-0}"         # 1 — без вопросов (значения из переменных окружения, остальное по умолчанию)
SKIP_DEPS="${SKIP_DEPS:-0}"           # для тестов: не ставить пакеты и Docker
SKIP_BUILD="${SKIP_BUILD:-0}"         # для тестов: не пересобирать образ
SKIP_FIREWALL="${SKIP_FIREWALL:-0}"   # для тестов: не трогать ufw
HTTPS_INSECURE="${HTTPS_INSECURE:-0}" # для тестов: не проверять сертификат
APP_DIR="$INSTALL_DIR/ai-council"
IMAGE="ai-council:latest"

# ------------------------------------------------------------------ вывод

if [[ -t 2 ]]; then
  C_STEP=$'\033[1;36m' C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_DIM=$'\033[2m' C_B=$'\033[1m' C_END=$'\033[0m'
else
  C_STEP="" C_OK="" C_WARN="" C_ERR="" C_DIM="" C_B="" C_END=""
fi
STEP_N=0
WARNINGS=()
step()  { STEP_N=$((STEP_N + 1)); printf '\n%s[%d] %s%s\n' "$C_STEP" "$STEP_N" "$1" "$C_END" >&2; }
ok()    { printf '%s  ✓ %s%s\n' "$C_OK" "$1" "$C_END" >&2; }
info()  { printf '    %s\n' "$1" >&2; }
warn()  { printf '%s  ! %s%s\n' "$C_WARN" "$1" "$C_END" >&2; WARNINGS+=("$1"); }
oops()  { printf '%s  ! %s%s\n' "$C_WARN" "$1" "$C_END" >&2; }   # ошибка ввода: показать, но не в итог
fail()  { printf '\n%s  ✗ %s%s\n' "$C_ERR" "$1" "$C_END" >&2; exit 1; }
guide() {  # guide "Заголовок" "строка" "строка"… — рамка с инструкцией
  local title="$1"; shift
  printf '\n%s  ┌─ %s%s\n' "$C_B" "$title" "$C_END" >&2
  local line; for line in "$@"; do printf '  │ %s\n' "$line" >&2; done
  printf '  └─\n' >&2
}
trap 'fail "Неожиданная ошибка в строке $LINENO. Запустите скрипт ещё раз; если повторится — пришлите вывод."' ERR

# ------------------------------------------------------------------ ввод (работает и при curl | bash)

TTY=""
if [[ "$ASSUME_YES" != 1 ]] && { : </dev/tty; } 2>/dev/null; then TTY=/dev/tty; fi

ask() {  # ask "Вопрос" "по умолчанию"
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
  answer="$(ask "$1 $hint" "" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$answer" ]] && { [[ "$def" == Y ]]; return; }
  [[ "$answer" =~ ^(y|yes|д|да)$ ]]
}
pause() { [[ -n "$TTY" ]] && { read -r -p "  ${1:-Нажмите Enter, чтобы продолжить…} " _ <"$TTY" || true; }; return 0; }
mask() { local s="$1"; ((${#s} > 10)) && printf '%s…%s' "${s:0:6}" "${s: -4}" || printf '***'; }
gen_password() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-16; }

# ------------------------------------------------------------------ .env и config.yaml

env_file() { printf '%s/.env' "$APP_DIR"; }
env_get() { [[ -f "$(env_file)" ]] && grep -E "^$1=" "$(env_file)" | tail -n1 | cut -d= -f2- | tr -d "\"'" || true; }
env_set() {
  local f tmp; f="$(env_file)"; tmp="$(mktemp)"
  touch "$f"
  awk -v k="$1" -v v="$2" 'BEGIN{d=0} index($0, k"=")==1 {print k"="v; d=1; next} {print} END{if(!d) print k"="v}' "$f" >"$tmp"
  cat "$tmp" >"$f"; rm -f "$tmp"; chmod 600 "$f"
}

cfg_py() {  # cfg_py 'код' — Python из образа, config.yaml доступен как /app/config.yaml (stdin передаётся)
  docker run --rm -i --entrypoint python -v "$APP_DIR/config.yaml:/app/config.yaml:ro" "$IMAGE" -c "$1"
}

# Правка config.yaml с сохранением комментариев:
#   replace <участник> <модель> | enable <участник> | disable <участник> | top <ключ> <значение>
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
            comment = re.search(r"\s+#.*$", l)
            lines[i] = f"{key}: {value}" + (f"  {comment.group(0).strip()}" if comment else "")
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
    en = next((i for i in block if re.match(r"\s+enabled\s*:", lines[i])), None)
    if action == "replace":
        lines[model_i] = f"{indent}model: {args[1]}"
    elif action == "disable":
        if en is None:
            lines.insert(model_i + 1, f"{indent}enabled: false")
        else:
            lines[en] = f"{indent}enabled: false"
    elif action == "enable" and en is not None:
        del lines[en]
with open(path, "w", encoding="utf-8") as f:   # запись поверх файла — не ломает монтирование в контейнер
    f.write("\n".join(lines))
PY
}

config_check() {  # «ok» или текст ошибки
  cfg_py '
from council.config import load_config
try:
    load_config("/app/config.yaml"); print("ok")
except Exception as e:
    print(str(e).replace("\n", " ")[:400])' </dev/null
}

# ------------------------------------------------------------------ HTTP

TG_TOKEN=""
tg() {  # tg метод '{json}' [таймаут]
  local data="${2:-}"; [[ -n "$data" ]] || data='{}'
  curl -sS --max-time "${3:-40}" -H 'Content-Type: application/json' -d "$data" \
    "$TELEGRAM_API/bot$TG_TOKEN/$1" 2>/dev/null
}
tg_send() {
  local body; body="$(jq -n --arg c "$1" --arg t "$2" '{chat_id: ($c|tonumber), text: $t, disable_web_page_preview: true}')"
  tg sendMessage "$body" >/dev/null || true
}
or_status() {  # HTTP-код запроса к OpenRouter с ключом
  curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H "Authorization: Bearer $1" "$OPENROUTER_API/$2" 2>/dev/null || echo 000
}
dc() { (cd "$APP_DIR" && docker compose "$@"); }
public_ip() {
  [[ -n "${PUBLIC_IP:-}" ]] && { printf '%s' "$PUBLIC_IP"; return; }
  local ip url
  for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
    ip="$(curl -s --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { printf '%s' "$ip"; return; }
  done
  hostname -I 2>/dev/null | awk '{print $1}' || true
}

# ------------------------------------------------------------------ шаги

intro() {
  printf '\n%s  Установка «Совета нейросетей»%s\n' "$C_STEP" "$C_END" >&2
  printf '%s  Несколько нейросетей решают вашу задачу как совет экспертов и выбирают лучший вариант.%s\n' "$C_DIM" "$C_END" >&2
  guide "Что понадобится (если чего-то нет — по ходу покажу, как получить)" \
    "1. Аккаунт OpenRouter с ключом и балансом от \$5 — через него работают все нейросети" \
    "2. Telegram-бот — создаётся за минуту у @BotFather" \
    "3. Telegram на телефоне или компьютере — чтобы подтвердить, что бот ваш" \
    "4. По желанию: свой домен для веб-интерфейса (если нет — дам бесплатный адрес)" \
    "" \
    "Займёт 10–15 минут. Ключи и пароли остаются только на этом сервере."
  pause "Нажмите Enter, чтобы начать…"
}

check_system() {
  step "Проверка сервера"
  [[ $EUID -eq 0 ]] || fail "Нужны права root. Запустите так: curl -fsSL $INSTALL_URL | sudo bash"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release; ok "Система: ${PRETTY_NAME:-неизвестно}"
  fi
  local mem; mem="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  ((mem == 0 || mem >= 700)) || warn "Памяти ${mem} МБ — может не хватить для сборки (лучше от 1 ГБ)"
}

install_deps() {
  step "Установка Git и Docker"
  if [[ "$SKIP_DEPS" == 1 ]]; then ok "Пропущено (SKIP_DEPS=1)"; return; fi
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git curl jq ca-certificates >/dev/null
  fi
  local cmd
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
      ok "Программа обновлена в $INSTALL_DIR"
    else
      warn "Не удалось обновить программу (возможно, файлы меняли вручную) — продолжаю с текущей версией"
    fi
  else
    git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || fail "Не удалось скачать $REPO_URL"
    ok "Программа скачана в $INSTALL_DIR"
  fi
  [[ -f "$APP_DIR/docker-compose.yml" ]] || fail "В $APP_DIR нет docker-compose.yml — проверьте ветку $BRANCH"
  [[ -f "$(env_file)" ]] || { cp "$APP_DIR/.env.example" "$(env_file)" 2>/dev/null || touch "$(env_file)"; chmod 600 "$(env_file)"; }
}

guide_openrouter() {
  guide "Как получить ключ OpenRouter" \
    "OpenRouter — единый доступ к Claude, ChatGPT, Gemini, Kimi, DeepSeek и другим моделям по одному ключу." \
    "" \
    "1. Откройте https://openrouter.ai и нажмите «Sign in» — вход через Google, GitHub или почту." \
    "2. Пополните баланс: https://openrouter.ai/settings/credits → «Add Credits»." \
    "   Для начала хватит \$5–10: одно заседание обычно стоит \$0.05–0.5." \
    "   Принимаются банковские карты (российские, как правило, нет) и криптовалюта." \
    "3. Создайте ключ: https://openrouter.ai/settings/keys → «Create Key»." \
    "   Название — любое, например ai-council. В поле «Credit limit» можно ограничить расходы по ключу." \
    "4. Скопируйте ключ — он начинается с sk-or-v1- и показывается только один раз."
}

setup_openrouter() {
  step "Доступ к нейросетям (OpenRouter)"
  local key existing code tries=0
  existing="$(env_get OPENROUTER_API_KEY)"
  key="${OPENROUTER_API_KEY:-}"
  if [[ -z "$key" && -n "$existing" ]] && confirm "Оставить сохранённый ключ $(mask "$existing")?" Y; then key="$existing"; fi
  if [[ -z "$key" && -n "$TTY" ]] && ! confirm "У вас уже есть ключ OpenRouter?" N; then
    guide_openrouter; pause "Когда ключ будет скопирован, нажмите Enter…"
  fi
  while :; do
    if [[ -z "$key" ]]; then
      key="$(ask_secret "Вставьте ключ OpenRouter (ввод скрыт; Enter без ключа — только демо-режим):")"
    fi
    if [[ -z "$key" ]]; then
      warn "Ключ OpenRouter не задан — бот будет работать только в демо-режиме. Добавить позже: ai-council reconfigure"
      env_set OPENROUTER_API_KEY ""; HAS_KEY=0; return
    fi
    if [[ "$key" != sk-or-* ]]; then
      oops "Это не похоже на ключ OpenRouter: он должен начинаться с sk-or-v1-"
      key=""; [[ -z "$TTY" ]] && fail "Неверный формат ключа OpenRouter"; continue
    fi
    code="$(or_status "$key" key)"
    [[ "$code" == 404 ]] && code="$(or_status "$key" auth/key)"
    if [[ "$code" == 200 ]]; then ok "Ключ принят"; break; fi
    if [[ "$code" == 401 || "$code" == 403 ]]; then
      tries=$((tries + 1))
      ((tries >= 3)) && fail "OpenRouter не принимает ключ. Создайте новый на https://openrouter.ai/settings/keys"
      oops "OpenRouter не принял ключ (HTTP $code). Скопируйте его ещё раз целиком."
      key=""; [[ -z "$TTY" ]] && fail "Неверный ключ OpenRouter"; continue
    fi
    warn "Не удалось проверить ключ (ответ: $code) — сохраняю как есть. Если OpenRouter недоступен с этого сервера, модели не будут отвечать."
    break
  done
  env_set OPENROUTER_API_KEY "$key"
  HAS_KEY=1
  local body left
  while :; do
    body="$(curl -sS --max-time 20 -H "Authorization: Bearer $key" "$OPENROUTER_API/credits" 2>/dev/null || true)"
    left="$(printf '%s' "$body" | jq -r 'try ((.data.total_credits // empty) - (.data.total_usage // 0)) catch empty' 2>/dev/null || true)"
    [[ -z "$left" ]] && return
    if awk -v x="$left" 'BEGIN{exit !(x > 0.01)}'; then ok "Баланс OpenRouter: \$$(printf '%.2f' "$left")"; return; fi
    guide "Баланс OpenRouter пуст" \
      "Без денег на счету модели не ответят." \
      "Пополните: https://openrouter.ai/settings/credits → «Add Credits» (для начала \$5–10)."
    if [[ -z "$TTY" ]] || ! confirm "Пополнили? Проверить баланс ещё раз?" Y; then
      warn "Баланс OpenRouter нулевой — пополните его, иначе модели не ответят: https://openrouter.ai/settings/credits"
      return
    fi
  done
}

guide_botfather() {
  guide "Как создать Telegram-бота" \
    "1. Откройте в Telegram @BotFather (официальный, с синей галочкой): https://t.me/BotFather" \
    "2. Нажмите «Запустить» и отправьте команду /newbot" \
    "3. Введите имя бота — как он будет называться в чатах, например: Совет нейросетей" \
    "4. Введите адрес (username) латиницей, обязательно на «bot», например: my_ai_council_bot" \
    "   Если адрес занят, BotFather попросит другой." \
    "5. BotFather пришлёт токен вида 1234567890:AAH…xyz — скопируйте его целиком." \
    "" \
    "Бот уже есть, но токен потерян: @BotFather → /mybots → ваш бот → API Token."
}

setup_telegram() {
  step "Telegram-бот"
  local token existing resp tries=0
  existing="$(env_get TELEGRAM_BOT_TOKEN)"
  token="${TELEGRAM_BOT_TOKEN:-}"
  if [[ -z "$token" && -n "$existing" ]] && confirm "Оставить сохранённый токен бота $(mask "$existing")?" Y; then token="$existing"; fi
  if [[ -z "$token" && -n "$TTY" ]] && ! confirm "Бот в Telegram уже создан?" N; then
    guide_botfather; pause "Когда токен будет скопирован, нажмите Enter…"
  fi
  while :; do
    if [[ -z "$token" ]]; then
      token="$(ask_secret "Вставьте токен бота (ввод скрыт):")"
      [[ -n "$token" ]] || fail "Без токена бот работать не может. Создайте бота у @BotFather и запустите скрипт снова."
    fi
    if [[ ! "$token" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{20,}$ ]]; then
      oops "Это не похоже на токен бота: он выглядит как 1234567890:AAH…xyz"
      token=""; [[ -z "$TTY" ]] && fail "Неверный формат токена бота"; continue
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
    ((tries >= 3)) && fail "Telegram не принимает токен. Проверьте его: @BotFather → /mybots → бот → API Token."
    oops "Telegram не принял токен: $(printf '%s' "$resp" | jq -r '.description // "ошибка"'). Скопируйте его ещё раз."
    token=""; [[ -z "$TTY" ]] && fail "Неверный токен бота"
  done
  env_set TELEGRAM_BOT_TOKEN "$token"
  local hook
  hook="$(tg getWebhookInfo '{}' 20 | jq -r '.result.url // ""' 2>/dev/null || true)"
  if [[ -n "$hook" ]]; then
    warn "У бота настроен webhook ($hook) — с ним бот не получит сообщения"
    if confirm "Отключить webhook?" Y; then tg deleteWebhook '{}' >/dev/null && ok "Webhook отключён"; fi
  fi
}

build_image() {
  step "Сборка программы"
  if [[ "$SKIP_BUILD" == 1 ]]; then ok "Пропущено (SKIP_BUILD=1)"; return; fi
  info "При первом запуске это займёт 1–3 минуты…"
  dc build -q bot >/tmp/ai-council-build.log 2>&1 || { tail -n 20 /tmp/ai-council-build.log >&2; fail "Сборка не удалась (подробности выше)"; }
  rm -f /tmp/ai-council-build.log
  ok "Программа собрана"
}

MODELS_JSON=""
load_models() {
  MODELS_JSON="$(curl -sS --max-time 30 "$OPENROUTER_API/models" 2>/dev/null || true)"
  printf '%s' "$MODELS_JSON" | jq -e '.data | length > 0' >/dev/null 2>&1 || MODELS_JSON=""
}

choose_members() {
  step "Участники совета"
  load_models
  local table
  # shellcheck disable=SC2016  # «$» — внутри Python-кода, не bash
  table="$(printf '%s' "${MODELS_JSON:-"{}"}" | cfg_py '
import json, sys, yaml
prices = {m["id"]: m.get("pricing") or {} for m in json.load(sys.stdin).get("data", [])}
cfg = yaml.safe_load(open("/app/config.yaml"))
for i, m in enumerate(cfg["members"], 1):
    p = prices.get(m["model"])
    price = "—"
    if p:
        inp = float(p.get("prompt") or 0) * 1e6
        out = float(p.get("completion") or 0) * 1e6
        price = f"${inp:.2f} / ${out:.2f}"
    print(i, m["name"], m["model"], "1" if m.get("enabled", True) else "0", price, sep="\t")
')"
  info "Каждая модель — отдельный «эксперт». Больше разных моделей — разнообразнее мнения, но дороже."
  info "Цена — за 1 млн токенов (вход / выход); заседание расходует примерно 10–30 тыс. на участника."
  printf '\n' >&2
  local n name model en price current=()
  while IFS=$'\t' read -r n name model en price; do
    [[ -z "$n" ]] && continue
    [[ "$en" == 1 ]] && current+=("$n")
    printf '    %2s. %s %-11s %-32s %s\n' "$n" "$([[ "$en" == 1 ]] && echo "✅" || echo "▫️")" "$name" "$model" "$price" >&2
  done <<<"$table"
  printf '\n' >&2
  local answer valid x
  local -A pick=()
  while :; do
    answer="$(ask "Номера участников через пробел (Enter — оставить отмеченные: ${current[*]}):" "${MEMBERS:-${current[*]}}")"
    answer="$(printf '%s' "$answer" | tr ',;' '  ')"
    valid=1; pick=()
    for x in $answer; do
      if [[ "$x" =~ ^[0-9]+$ ]] && grep -q "^$x"$'\t' <<<"$table"; then pick[$x]=1; else valid=0; fi
    done
    if ((valid == 0)); then oops "Непонятные номера: $answer"; [[ -z "$TTY" ]] && fail "Неверный список участников"; continue; fi
    if ((${#pick[@]} < 2)); then oops "Нужно минимум 2 участника"; [[ -z "$TTY" ]] && fail "Слишком мало участников"; continue; fi
    break
  done
  local names=()
  while IFS=$'\t' read -r n name model en price; do
    [[ -z "$n" ]] && continue
    if [[ -n "${pick[$n]:-}" ]]; then
      [[ "$en" == 1 ]] || edit_config enable "$name"
      names+=("$name")
    elif [[ "$en" == 1 ]]; then
      edit_config disable "$name"
    fi
  done <<<"$table"
  ok "В совете: ${names[*]}"
}

check_models() {
  step "Проверка моделей на OpenRouter"
  local missing name model sugg
  if [[ -z "$MODELS_JSON" ]]; then
    warn "Не удалось получить список моделей OpenRouter — проверка пропущена"
  else
    missing="$(printf '%s' "$MODELS_JSON" | cfg_py '
import difflib, json, sys
from council.config import load_config
known = sorted(m["id"] for m in json.load(sys.stdin)["data"])
for m in load_config("/app/config.yaml").active_members:
    if m.model not in known:
        s = difflib.get_close_matches(m.model, known, n=1, cutoff=0.5) or [""]
        print(m.name, m.model, s[0], sep="\t")
' || true)"
    if [[ -z "$missing" ]]; then
      ok "Все выбранные модели есть на OpenRouter"
    else
      while IFS=$'\t' read -r name model sugg; do
        [[ -z "$name" ]] && continue
        if [[ -n "$sugg" ]] && confirm "Модели «$model» ($name) нет на OpenRouter. Заменить на «$sugg»?" Y; then
          edit_config replace "$name" "$sugg" && ok "$name: $model → $sugg"
        else
          edit_config disable "$name" && warn "$name отключён: модели «$model» нет на OpenRouter (вернуть: ai-council config)"
        fi
      done <<<"$missing"
    fi
  fi
  local res first
  res="$(config_check)"
  if [[ "$res" == *председатель* ]]; then
    first="$(cfg_py '
import yaml; d = yaml.safe_load(open("/app/config.yaml"))
print(next(m["name"] for m in d["members"] if m.get("enabled", True)))' </dev/null)"
    edit_config top chair "$first" && warn "Председатель заменён на $first"
    res="$(config_check)"
  fi
  if [[ "$res" == *"адвокат"* ]]; then
    edit_config top devil_advocate null && warn "Адвокат дьявола отключён"
    res="$(config_check)"
  fi
  [[ "$res" == ok ]] || fail "Ошибка в config.yaml: $res. Исправьте: nano $APP_DIR/config.yaml"
  local roles chair devil
  roles="$(cfg_py '
from council.config import load_config
c = load_config("/app/config.yaml"); print(c.chair, c.devil_advocate or "—", sep="\t")' </dev/null)"
  IFS=$'\t' read -r chair devil <<<"$roles"
  ok "Председатель: $chair, адвокат дьявола: $devil"
}

setup_budget() {
  step "Лимит расходов"
  local current value
  current="$(cfg_py 'import yaml; print(yaml.safe_load(open("/app/config.yaml")).get("max_cost_usd", 2))' </dev/null)"
  info "Это жёсткий предел стоимости одного заседания: если он достигнут, заседание остановится."
  info "Обычно заседание стоит \$0.05–0.5 (быстрый режим дешевле, дебаты в 2–3 раунда дороже)."
  while :; do
    value="$(ask "Лимит на одно заседание в долларах [$current]:" "${BUDGET:-$current}" | tr ',' '.')"
    if [[ "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v x="$value" 'BEGIN{exit !(x >= 0.05 && x <= 100)}'; then break; fi
    oops "Нужно число от 0.05 до 100, например 1 или 0.5"
    [[ -z "$TTY" ]] && fail "Неверный лимит"
  done
  [[ "$value" == "$current" ]] || edit_config top max_cost_usd "$value"
  ok "Лимит заседания: \$$value"
  info "Совет: задайте ещё общий лимит на сам ключ — https://openrouter.ai/settings/keys → ключ → Credit limit."
}

wait_for_message() {  # печатает «id<TAB>имя<TAB>username» первого личного сообщения боту или ничего
  local wait="$1" resp offset="" deadline found="" last
  resp="$(tg getUpdates '{"timeout":0}' 20 || true)"
  if [[ "$(printf '%s' "$resp" | jq -r '.error_code // 0' 2>/dev/null)" == 409 ]]; then
    fail "Этот бот уже запущен где-то ещё (например, на вашем компьютере). Остановите его там и запустите скрипт снова."
  fi
  offset="$(printf '%s' "$resp" | jq -r '[.result[]?.update_id] | max // empty | . + 1' 2>/dev/null || true)"
  deadline=$((SECONDS + wait))
  while ((SECONDS < deadline)); do
    resp="$(tg getUpdates "{\"timeout\":25${offset:+,\"offset\":$offset}}" 35 || true)"
    [[ -z "$resp" ]] && { sleep 3; continue; }
    [[ "$(printf '%s' "$resp" | jq -r '.error_code // 0')" == 409 ]] && fail "Этот бот уже запущен где-то ещё. Остановите другой экземпляр и запустите скрипт снова."
    last="$(printf '%s' "$resp" | jq -r '[.result[]?.update_id] | max // empty')"
    [[ -n "$last" ]] && offset=$((last + 1))
    found="$(printf '%s' "$resp" | jq -r '[.result[]? | (.message // .edited_message // empty) | select(.chat.type == "private") | .from]
               | first // empty | "\(.id)\t\(.first_name // "-")\t\(.username // "")"')"
    if [[ -n "$found" ]]; then
      tg getUpdates "{\"timeout\":0,\"offset\":$offset}" 20 >/dev/null || true   # пометить прочитанным
      printf '%s' "$found"; return
    fi
  done
}

catch_user() {  # catch_user "кто" -> ID в stdout (или пусто)
  local who="$1" found uid name uname
  printf '\n%s  ➜ %s: откройте Telegram и напишите боту @%s любое сообщение, например «привет».%s\n' \
    "$C_STEP" "$who" "$BOT_USERNAME" "$C_END" >&2
  info "Ссылка на бота: https://t.me/$BOT_USERNAME"
  if ((ID_WAIT >= 60)); then info "Жду до $((ID_WAIT / 60)) мин…"; else info "Жду до $ID_WAIT с…"; fi
  found="$(wait_for_message "$ID_WAIT")"
  if [[ -z "$found" ]]; then warn "Сообщение не пришло за отведённое время"; return 0; fi
  IFS=$'\t' read -r uid name uname <<<"$found"
  if confirm "Сообщение от: ${name}${uname:+ (@$uname)}, ID $uid. Дать доступ?" Y; then printf '%s' "$uid"; fi
}

setup_users() {
  step "Кто может пользоваться ботом"
  info "Бот тратит ваш баланс OpenRouter, поэтому отвечает только тем, кому вы открыли доступ."
  local existing allowed uid
  existing="$(env_get TELEGRAM_ALLOWED_USERS)"
  allowed="${TELEGRAM_ALLOWED_USERS:-}"
  if [[ -z "$allowed" && -n "$existing" ]] && confirm "Оставить доступ для ID: $existing?" Y; then allowed="$existing"; fi
  dc stop bot >/dev/null 2>&1 || true   # пока бот запущен, он сам забирает сообщения
  if [[ -z "$allowed" ]]; then
    uid="$(catch_user "Вы")"
    if [[ -n "$uid" ]]; then
      allowed="$uid"
      tg_send "$uid" "✅ Доступ открыт. Ещё пара минут — и совет будет готов к работе."
    fi
    while [[ ! "$allowed" =~ ^-?[0-9]+(,-?[0-9]+)*$ ]]; do
      [[ -z "$TTY" ]] && fail "Не удалось определить ваш Telegram ID. Задайте его: TELEGRAM_ALLOWED_USERS=<id>"
      info "Свой ID можно узнать у бота @userinfobot (он ответит числом)."
      allowed="$(ask "Введите ваш Telegram ID:" "" | tr -d '[:space:]')"
    done
  fi
  while [[ -n "$TTY" ]] && confirm "Дать доступ ещё кому-нибудь (семья, коллеги)?" N; do
    info "Попросите человека найти бота @$BOT_USERNAME (ссылка: https://t.me/$BOT_USERNAME) и написать ему."
    uid="$(catch_user "Другой человек")"
    if [[ -z "$uid" ]]; then
      uid="$(ask "Или введите его Telegram ID вручную (Enter — пропустить):" "" | tr -d '[:space:]')"
    fi
    if [[ "$uid" =~ ^-?[0-9]+$ ]]; then
      [[ ",$allowed," == *",$uid,"* ]] || allowed="$allowed,$uid"
      tg_send "$uid" "✅ Вам открыт доступ к «Совету нейросетей». Скоро бот заработает."
      ok "Добавлен ID $uid"
    fi
  done
  env_set TELEGRAM_ALLOWED_USERS "$allowed"
  ALLOWED="$allowed"
  ok "Доступ у ID: $allowed"
}

guide_dns() {
  guide "Как направить домен на сервер" \
    "Откройте управление DNS у регистратора домена (Reg.ru, Namecheap, Cloudflare и т. п.):" \
    "1. Добавьте запись типа A." \
    "   Имя: @ — для самого домена, или поддомен, например council (будет council.ваш-домен.ru)." \
    "   Значение: $1" \
    "2. Если домен на Cloudflare — выключите для записи «оранжевое облако» (Proxy status: DNS only)." \
    "3. Сохраните. Обычно запись начинает работать через 5–30 минут."
}

resolves_to() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | grep -qx "$2"; }

setup_web() {
  step "Веб-интерфейс"
  info "Это то же, что бот, но в браузере: ход дебатов вживую, таблица оценок, история заседаний."
  local mode existing_mode choice def=1
  existing_mode="$(env_get WEB_MODE)"
  mode="${WEB_MODE:-}"
  if [[ -z "$mode" ]]; then
    printf '\n    1) Из любого места по адресу https://… с логином и паролем (рекомендуется)\n' >&2
    printf '    2) Не нужен — только Telegram-бот\n' >&2
    printf '    3) Только через SSH-туннель (для продвинутых)\n\n' >&2
    case "$existing_mode" in none) def=2 ;; tunnel) def=3 ;; esac
    while :; do
      choice="$(ask "Ваш выбор [$def]:" "$def")"
      case "$choice" in 1) mode=https; break ;; 2) mode=none; break ;; 3) mode=tunnel; break ;; esac
      oops "Введите 1, 2 или 3"; [[ -z "$TTY" ]] && fail "Неверный выбор"
    done
  fi
  [[ "$mode" =~ ^(none|tunnel|https)$ ]] || fail "WEB_MODE должен быть none, tunnel или https"
  env_set WEB_MODE "$mode"
  WEB_MODE="$mode"
  case "$mode" in
    none)   ok "Веб-интерфейс не нужен (включить позже: ai-council reconfigure)"; return ;;
    tunnel) ok "Веб-интерфейс будет доступен через SSH-туннель"; return ;;
  esac

  local ip domain existing_domain
  ip="$(public_ip)"
  [[ -n "$ip" ]] || fail "Не удалось определить IP сервера"
  existing_domain="$(env_get DOMAIN)"
  domain="${DOMAIN:-}"
  if [[ -z "$domain" && -n "$existing_domain" ]] && confirm "Оставить адрес $existing_domain?" Y; then domain="$existing_domain"; fi
  if [[ -z "$domain" ]]; then
    if confirm "Есть свой домен, который хотите использовать?" N; then
      domain="$(ask "Введите домен (например council.example.ru):" "" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
      domain="${domain#http://}"; domain="${domain#https://}"; domain="${domain%%/*}"
      if [[ ! "$domain" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]]; then oops "Странный домен «$domain» — использую бесплатный адрес"; domain=""; fi
    fi
    if [[ -z "$domain" ]]; then
      domain="${ip//./-}.sslip.io"
      info "Бесплатный адрес $domain уже указывает на ваш сервер — регистрировать ничего не нужно."
    fi
  fi
  if ! resolves_to "$domain" "$ip"; then
    guide_dns "$ip"
    while ! resolves_to "$domain" "$ip"; do
      if [[ -z "$TTY" ]]; then warn "Домен $domain пока не указывает на $ip — HTTPS может не заработать"; break; fi
      if confirm "Домен $domain пока не указывает на $ip. Проверить ещё раз? (н — взять бесплатный адрес)" Y; then
        info "Проверяю… (DNS иногда обновляется до 30 минут)"; sleep 10
      else
        domain="${ip//./-}.sslip.io"; info "Используем $domain"; break
      fi
    done
  fi
  env_set DOMAIN "$domain"
  DOMAIN="$domain"
  ok "Адрес: https://$domain"

  local user existing_user pass p2
  existing_user="$(env_get WEB_USER)"
  user="$(ask "Логин для входа [${existing_user:-admin}]:" "${WEB_USER:-${existing_user:-admin}}")"
  while [[ ! "$user" =~ ^[A-Za-z0-9._-]{3,32}$ ]]; do
    oops "Логин: 3–32 символа, латиница, цифры, точка, дефис, подчёркивание"
    [[ -z "$TTY" ]] && fail "Неверный логин"
    user="$(ask "Логин:" "admin")"
  done
  pass="${WEB_PASSWORD:-}"
  if [[ -z "$pass" && -n "$(env_get WEB_PASSWORD)" ]] && confirm "Оставить сохранённый пароль?" Y; then pass="$(env_get WEB_PASSWORD)"; fi
  if [[ -z "$pass" ]]; then
    if confirm "Сгенерировать надёжный пароль автоматически?" Y; then
      pass="$(gen_password)"
    else
      while :; do
        pass="$(ask_secret "Придумайте пароль (от 10 символов; ввод скрыт):")"
        p2="$(ask_secret "Повторите пароль:")"
        if [[ "$pass" != "$p2" ]]; then oops "Пароли не совпали"; continue; fi
        if [[ ${#pass} -lt 10 || ! "$pass" =~ ^[A-Za-z0-9._@%+=!^*-]+$ ]]; then
          oops "Нужно от 10 символов: латиница, цифры и . _ @ % + = ! ^ * -"; continue
        fi
        break
      done
    fi
  fi
  env_set WEB_USER "$user"
  env_set WEB_PASSWORD "$pass"
  WEB_USER="$user"; WEB_PASSWORD="$pass"
  ok "Вход: логин $user, пароль сохранён"

  if [[ "$SKIP_FIREWALL" != 1 ]] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ok "В брандмауэре открыты порты 80 и 443"
  fi
}

compose_profiles() {
  case "${WEB_MODE:-none}" in https) echo "--profile https" ;; tunnel) echo "--profile web" ;; *) echo "" ;; esac
}

start_services() {
  step "Запуск"
  local profiles; profiles="$(compose_profiles)"
  case "${WEB_MODE:-none}" in  # убрать то, что больше не нужно
    none)   dc --profile https rm -sf web caddy >/dev/null 2>&1 || true ;;
    tunnel) dc --profile https rm -sf caddy >/dev/null 2>&1 || true ;;
  esac
  # shellcheck disable=SC2086
  dc $profiles up -d --force-recreate >/tmp/ai-council-up.log 2>&1 || { tail -n 15 /tmp/ai-council-up.log >&2; fail "Не удалось запустить контейнеры (подробности выше)"; }
  rm -f /tmp/ai-council-up.log

  local deadline=$((SECONDS + 60)) logs=""
  while ((SECONDS < deadline)); do
    logs="$(dc logs --no-color bot 2>&1 || true)"
    if grep -q "бот @.* запущен" <<<"$logs"; then ok "Бот @$BOT_USERNAME работает"; break; fi
    grep -q "Неверный TELEGRAM_BOT_TOKEN" <<<"$logs" && fail "Бот не принял токен"
    sleep 2
  done
  if ! grep -q "бот @.* запущен" <<<"$logs"; then
    printf '%s\n' "$logs" | tail -n 15 >&2
    grep -q "Telegram недоступен" <<<"$logs" && fail "Бот не может подключиться к Telegram с этого сервера (выше логи)."
    fail "Бот не запустился за минуту (выше логи)"
  fi

  if [[ "${WEB_MODE:-none}" == tunnel ]]; then ok "Веб-интерфейс запущен (доступ через SSH-туннель)"; fi
  [[ "${WEB_MODE:-none}" == https ]] || return 0
  info "Получаю HTTPS-сертификат для $DOMAIN (обычно до минуты)…"
  local code="000" k=()
  [[ "$HTTPS_INSECURE" == 1 ]] && k=(-k)
  deadline=$((SECONDS + HTTPS_WAIT))
  while ((SECONDS < deadline)); do
    code="$(curl -s "${k[@]}" -o /dev/null -w '%{http_code}' --max-time 10 -u "$WEB_USER:$WEB_PASSWORD" "https://$DOMAIN/api/config" 2>/dev/null || echo 000)"
    [[ "$code" == 200 ]] && break
    sleep 5
  done
  if [[ "$code" == 200 ]]; then
    ok "Веб-интерфейс открывается: https://$DOMAIN (пароль проверен)"
    local nopass; nopass="$(curl -s "${k[@]}" -o /dev/null -w '%{http_code}' --max-time 10 "https://$DOMAIN/" 2>/dev/null || echo 000)"
    [[ "$nopass" == 401 ]] && ok "Без пароля вход закрыт"
  else
    dc --profile https logs --no-color --tail 15 caddy >&2 2>/dev/null || true
    warn "Веб-интерфейс по адресу https://$DOMAIN пока не открывается. Частые причины: в панели провайдера закрыты порты 80/443 или домен ещё не указывает на сервер. Бот при этом работает."
  fi
}

install_helper() {
  mkdir -p "$BIN_DIR"
  cat >"$BIN_DIR/ai-council" <<EOF
#!/usr/bin/env bash
# Управление «Советом нейросетей». Установлено скриптом install.sh.
set -e
cd "$APP_DIR"
mode="\$(grep -E '^WEB_MODE=' .env 2>/dev/null | tail -n1 | cut -d= -f2)"
case "\$mode" in https) P=(--profile https) ;; tunnel) P=(--profile web) ;; *) P=() ;; esac
dc() { docker compose "\${P[@]}" "\$@"; }
case "\${1:-help}" in
  status)  dc ps ;;
  logs)    docker compose --profile https logs -f --tail 100 "\${2:-bot}" ;;
  restart) dc up -d --force-recreate ;;
  stop)    docker compose --profile https down ;;
  start)   dc up -d ;;
  update)  git -C "$INSTALL_DIR" pull -q --ff-only --autostash && dc build -q && dc up -d --force-recreate && echo "Обновлено." ;;
  ask)     shift; docker compose run --rm bot python -m council ask "\$@" ;;
  config)  "\${EDITOR:-nano}" config.yaml && echo "Сохранено: изменения подхватятся со следующего заседания." ;;
  password)
    read -r -s -p "Новый пароль веб-интерфейса (от 10 символов): " p1 && echo
    read -r -s -p "Повторите: " p2 && echo
    [[ "\$p1" == "\$p2" && \${#p1} -ge 10 && "\$p1" =~ ^[A-Za-z0-9._@%+=!^*-]+\$ ]] || { echo "Пароли не совпали или не подходят"; exit 1; }
    tmp="\$(mktemp)"; awk -v v="\$p1" 'index(\$0,"WEB_PASSWORD=")==1{print "WEB_PASSWORD="v; next}{print}' .env >"\$tmp" && cat "\$tmp" >.env && rm -f "\$tmp"
    sed -i "s|^Пароль:.*|Пароль: \$p1|" "$ACCESS_FILE" 2>/dev/null || true
    dc up -d --force-recreate web && echo "Пароль изменён." ;;
  access)  cat "$ACCESS_FILE" ;;
  reconfigure) curl -fsSL "$INSTALL_URL" | bash ;;
  backup)  d="runs-backup-\$(date +%F)"; docker compose cp bot:/app/runs "\$d" && echo "Отчёты скопированы в $APP_DIR/\$d" ;;
  *) cat <<'HELP'
ai-council — управление «Советом нейросетей»
  status         состояние              logs [bot|web|caddy]  логи
  restart        перезапуск             stop / start          остановить / запустить
  update         обновить программу     reconfigure           заново пройти настройку
  config         участники и модели     password              сменить пароль веб-интерфейса
  access         адреса и пароли        backup                копия всех отчётов
  ask "вопрос"   заседание из консоли
HELP
  ;;
esac
EOF
  chmod +x "$BIN_DIR/ai-council"
}

write_access_file() {
  local ip=""; [[ "${WEB_MODE:-none}" == tunnel ]] && ip="$(public_ip)"
  (
    umask 077
    {
      echo "Совет нейросетей — данные доступа (файл виден только root)"
      echo "Создан: $(date '+%Y-%m-%d %H:%M')"
      echo
      echo "Telegram-бот: https://t.me/$BOT_USERNAME"
      echo "Доступ у Telegram ID: ${ALLOWED:-}"
      case "${WEB_MODE:-none}" in
        https)
          echo
          echo "Веб-интерфейс: https://$DOMAIN"
          echo "Логин: $WEB_USER"
          echo "Пароль: $WEB_PASSWORD" ;;
        tunnel)
          echo
          echo "Веб-интерфейс: ssh -L 8000:127.0.0.1:8000 root@$ip, затем http://localhost:8000" ;;
      esac
      echo
      echo "Ключ OpenRouter и токен бота: $APP_DIR/.env"
      echo "Участники и модели: $APP_DIR/config.yaml  (команда: ai-council config)"
      echo "Управление: ai-council help"
    } >"$ACCESS_FILE"
  )
  chmod 600 "$ACCESS_FILE"
}

summary() {
  local web_line="" first="${ALLOWED%%,*}"
  [[ "${WEB_MODE:-none}" == https ]] && web_line="
Веб-интерфейс: https://$DOMAIN (логин: $WEB_USER, пароль — у администратора сервера)"
  [[ -n "$first" ]] && tg_send "$first" "🏛 Совет нейросетей запущен на сервере и готов к работе.
Напишите любой вопрос — я покажу настройки и оценку стоимости. Справка: /help${web_line}"

  printf '\n%s══════════════════════════════════════════════════════%s\n' "$C_OK" "$C_END" >&2
  printf '%s ✅ Готово! Совет нейросетей работает.%s\n' "$C_OK" "$C_END" >&2
  printf '%s══════════════════════════════════════════════════════%s\n\n' "$C_OK" "$C_END" >&2
  info "${C_B}Telegram:${C_END} https://t.me/$BOT_USERNAME — напишите вопрос (бот уже прислал приветствие)"
  case "${WEB_MODE:-none}" in
    https)
      info "${C_B}Веб-интерфейс:${C_END} https://$DOMAIN"
      info "${C_B}Логин:${C_END} $WEB_USER   ${C_B}Пароль:${C_END} $WEB_PASSWORD"
      info "${C_WARN}Сохраните пароль в надёжном месте.${C_END} Посмотреть ещё раз: ai-council access" ;;
    tunnel)
      info "${C_B}Веб-интерфейс:${C_END} на своём компьютере выполните  ssh -L 8000:127.0.0.1:8000 root@$(public_ip)"
      info "               и откройте http://localhost:8000" ;;
  esac
  [[ "${HAS_KEY:-0}" == 1 ]] || info "${C_WARN}Ключа OpenRouter нет — работает только демо-режим. Добавить: ai-council reconfigure${C_END}"
  info ""
  info "Все адреса и пароли сохранены в $ACCESS_FILE (виден только root)."
  info "Управление: ${C_B}ai-council${C_END} — status, logs, restart, update, config, password, access…"
  info "Бот сам перезапускается после сбоев и перезагрузки сервера."
  if ((${#WARNINGS[@]})); then
    printf '\n%s  Обратите внимание:%s\n' "$C_WARN" "$C_END" >&2
    local w; for w in "${WARNINGS[@]}"; do printf '%s  • %s%s\n' "$C_WARN" "$w" "$C_END" >&2; done
  fi
  printf '\n' >&2
}

main() {
  intro
  check_system
  install_deps
  fetch_code
  setup_openrouter
  setup_telegram
  build_image
  choose_members
  check_models
  setup_budget
  setup_users
  setup_web
  start_services
  install_helper
  write_access_file
  summary
}

# Вся логика — в функциях, вызов в самом конце: bash успеет прочитать скрипт целиком даже при curl | bash.
if [[ "${AI_COUNCIL_SOURCE_ONLY:-0}" != 1 ]]; then main "$@"; fi
