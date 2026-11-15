#!/usr/bin/env bash
# Управление «Советом нейросетей» из веб-интерфейса: обновление программы и перезапуск.
#
# Веб-интерфейс работает в контейнере и не имеет доступа к Docker и серверу (так безопаснее).
# Он только оставляет заявку — файл control/request («update» или «restart»). Служба systemd
# на сервере замечает заявку, выполняет её этим скриптом и записывает ход работы в control/,
# откуда веб-интерфейс показывает статус и журнал.
#
#   sudo bash deploy/web-control.sh install     — один раз: служба, права на config.yaml, branding/, control/
#   sudo bash deploy/web-control.sh update-now  — обновить сейчас из терминала (то же, что кнопка в вебе)
#   bash deploy/web-control.sh run              — выполнить заявку (вызывает служба)
#   bash deploy/web-control.sh version          — записать сведения о версии
#   sudo bash deploy/web-control.sh backup-now  — резервная копия сейчас (сама делается каждую ночь)
#   sudo bash deploy/web-control.sh restore ФАЙЛ — восстановить из копии (backups/ai-council-….tgz)

set -Euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(git -C "$APP_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$APP_DIR")"
CONTROL="$APP_DIR/control"
APP_UID=1000   # пользователь внутри контейнера (см. Dockerfile)
UNIT=ai-council-control
LOG="$CONTROL/update.log"
BACKUPS="$APP_DIR/backups"     # резервные копии: ключи, настройки, логотип, архив заседаний, проекты
BACKUP_LOG="$CONTROL/backup.log"

compose() {
  local mode P=()
  mode="$(grep -E '^WEB_MODE=' "$APP_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
  case "$mode" in https) P=(--profile https) ;; tunnel) P=(--profile web) ;; esac
  (cd "$APP_DIR" && docker compose "${P[@]}" "$@")
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))' 2>/dev/null \
  || sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/^/"/' -e 's/$/"/' | tr -d '\n'; }

write_status() {  # write_status состояние действие сообщение
  mkdir -p "$CONTROL"
  local msg; msg="$(printf '%s' "$3" | json_escape)"
  printf '{"state": "%s", "action": "%s", "message": %s, "at": "%s"}\n' "$1" "$2" "$msg" "$(now)" >"$CONTROL/status.json.tmp"
  mv -f "$CONTROL/status.json.tmp" "$CONTROL/status.json"
  chmod 644 "$CONTROL/status.json" 2>/dev/null || true
}

write_version() {
  mkdir -p "$CONTROL"
  local commit date subject branch remote
  commit="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  date="$(git -C "$REPO_DIR" log -1 --format=%cI 2>/dev/null || echo '')"
  subject="$(git -C "$REPO_DIR" log -1 --format=%s 2>/dev/null | json_escape)"
  branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  remote="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo '')"
  printf '{"commit": "%s", "date": "%s", "subject": %s, "branch": "%s", "remote": "%s", "written": "%s"}\n' \
    "$commit" "$date" "${subject:-\"\"}" "$branch" "$remote" "$(now)" >"$CONTROL/version.json"
  chmod 644 "$CONTROL/version.json" 2>/dev/null || true
}

fix_permissions() {
  mkdir -p "$CONTROL" "$APP_DIR/branding"
  chown "$APP_UID" "$CONTROL" "$APP_DIR/config.yaml" 2>/dev/null || true
  chown -R "$APP_UID" "$APP_DIR/branding" 2>/dev/null || true
  chmod 755 "$CONTROL"; chmod 644 "$APP_DIR/config.yaml"
}

ensure_swap() {
  # На маленьком сервере (меньше 2 ГБ памяти) без подкачки вёрстка длинных отчётов и сборка образа могут
  # исчерпать память — тогда сервер «замирает» целиком. Файл подкачки на 2 ГБ превращает это в замедление.
  local mem swap free_mb
  mem="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  swap="$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  ((mem > 0 && mem < 2000 && swap < 500)) || return 0
  [[ -e /swapfile ]] && return 0
  free_mb="$(df -Pm / | awk 'NR==2 {print $4}')"
  ((free_mb > 4000)) || { echo "!! Памяти ${mem} МБ и нет подкачки, но на диске мало места (${free_mb} МБ) — подкачку не создаю"; return 0; }
  echo "== Памяти ${mem} МБ и нет подкачки — включаю файл подкачки 2 ГБ (/swapfile)"
  if { fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; } \
      && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -q vm.swappiness=10 2>/dev/null || true
    echo "== Подкачка включена"
  else
    rm -f /swapfile
    echo "!! Не удалось включить подкачку (на некоторых VPS это запрещено) — продолжаю без неё"
  fi
  return 0
}

do_update() {
  local branch cfg_backup
  branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
  echo "== Обновление программы ($branch) — $(now)"
  # Ваши настройки (config.yaml) — главнее версии из репозитория: убираем их на время обновления и возвращаем.
  cfg_backup="$(mktemp)"
  cp "$APP_DIR/config.yaml" "$cfg_backup"
  git -C "$REPO_DIR" checkout -q -- "$APP_DIR/config.yaml" 2>/dev/null || true
  if ! git -C "$REPO_DIR" fetch -q origin "$branch" || ! git -C "$REPO_DIR" merge -q --ff-only "origin/$branch"; then
    cp "$cfg_backup" "$APP_DIR/config.yaml"; rm -f "$cfg_backup"
    echo "!! Не удалось получить обновление (нет связи с GitHub или файлы программы меняли вручную)."
    return 1
  fi
  cp "$cfg_backup" "$APP_DIR/config.yaml"; rm -f "$cfg_backup"
  echo "== Код обновлён до $(git -C "$REPO_DIR" log -1 --format='%h %s')"
  fix_permissions
  ensure_swap
  echo "== Сборка образа (может занять несколько минут)"
  compose build -q || { echo "!! Ошибка сборки образа"; return 1; }
  echo "== Перезапуск"
  compose up -d --force-recreate || { echo "!! Ошибка перезапуска"; return 1; }
  write_version
  install_backup_timer
  echo "== Готово"
}

env_get() { grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

do_backup() {
  # Копия всего, что нужно для восстановления на любом сервере: .env (ключи), config.yaml, branding/ и данные
  # программы (архив заседаний, отчёты, проекты закупки, копии страниц). Хранятся последние BACKUP_KEEP (14).
  local keep stamp tmp out remote free need vol
  keep="$(env_get BACKUP_KEEP)"; [[ "$keep" =~ ^[0-9]+$ ]] || keep=14
  stamp="$(date +%Y-%m-%d_%H%M)"
  out="$BACKUPS/ai-council-$stamp.tgz"
  echo "== Резервная копия — $(now)"
  mkdir -p "$BACKUPS"
  tmp="$(mktemp -d)"
  if ! compose cp bot:/app/runs "$tmp/runs" >/dev/null 2>&1; then
    # бот остановлен — данные берём прямо из тома Docker
    vol="$(docker volume ls -q | grep -E '(^|_)council-runs$' | head -n1)"
    if [[ -z "$vol" ]] || ! docker run --rm -v "$vol:/from:ro" -v "$tmp:/to" alpine sh -c 'cp -a /from /to/runs' >/dev/null 2>&1; then
      echo "!! Не удалось скопировать данные программы"; rm -rf "$tmp"; return 1
    fi
  fi
  cp "$APP_DIR/.env" "$APP_DIR/config.yaml" "$tmp/" 2>/dev/null || true
  if [[ -d "$APP_DIR/branding" ]]; then cp -r "$APP_DIR/branding" "$tmp/branding"; fi
  need="$(du -sm "$tmp" | cut -f1)"; free="$(df -Pm "$BACKUPS" | awk 'NR==2 {print $4}')"
  if ((free < need + 500)); then
    echo "!! Мало места на диске (свободно ${free} МБ, нужно ~${need} МБ) — копия не сделана"; rm -rf "$tmp"; return 1
  fi
  if ! tar czf "$out.part" -C "$tmp" .; then
    echo "!! Не удалось упаковать копию"; rm -rf "$tmp" "$out.part"; return 1
  fi
  mv -f "$out.part" "$out"; rm -rf "$tmp"
  # в копии ключи: файлы читает только владелец (сайт в контейнере — пользователь 1000 — отдаёт их на скачивание)
  chown -R "$APP_UID" "$BACKUPS"; chmod 700 "$BACKUPS"; chmod 600 "$BACKUPS"/ai-council-*.tgz
  ls -1t "$BACKUPS"/ai-council-*.tgz 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f
  # по желанию — копия вне сервера (rclone: Google Drive, Яндекс Диск, S3…; BACKUP_RCLONE=имя:папка в .env)
  remote="$(env_get BACKUP_RCLONE)"
  if [[ -n "$remote" ]]; then
    if command -v rclone >/dev/null 2>&1 && rclone copy "$out" "$remote" >/dev/null 2>&1; then
      echo "== Копия отправлена в $remote"
    else
      echo "!! Копия не отправлена в $remote (нет rclone или он не настроен) — на сервере она есть"
    fi
  fi
  echo "== Готово: $(basename "$out"), $(du -h "$out" | cut -f1); хранятся последние $keep"
}

do_restore() {  # do_restore ФАЙЛ — восстановление поверх текущих данных
  local file="$1" tmp
  [[ -f "$file" ]] || { echo "!! Нет файла $file"; return 1; }
  echo "== Восстановление из $(basename "$file") — $(now)"
  tmp="$(mktemp -d)"
  tar xzf "$file" -C "$tmp" || { echo "!! Не удалось распаковать копию"; rm -rf "$tmp"; return 1; }
  if [[ -f "$tmp/.env" ]]; then cp "$tmp/.env" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"; fi
  if [[ -f "$tmp/config.yaml" ]]; then cp "$tmp/config.yaml" "$APP_DIR/config.yaml"; fi
  if [[ -d "$tmp/branding" ]]; then mkdir -p "$APP_DIR/branding"; cp -r "$tmp/branding/." "$APP_DIR/branding/"; fi
  fix_permissions
  compose up -d bot >/dev/null
  if [[ -d "$tmp/runs" ]]; then
    compose cp "$tmp/runs/." bot:/app/runs || { echo "!! Не удалось вернуть данные программы"; rm -rf "$tmp"; return 1; }
  fi
  rm -rf "$tmp"
  compose up -d --force-recreate >/dev/null   # при запуске программа вернёт себе права на данные
  echo "== Готово: данные восстановлены, программа перезапущена"
}

install_backup_timer() {  # ночная резервная копия (systemd); повторный вызов ничего не ломает
  if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then return 0; fi
  printf '%s\n' "[Unit]" "Description=AI Council: резервная копия (ключи, настройки, архив заседаний)" "" \
    "[Service]" "Type=oneshot" "ExecStart=/usr/bin/env bash $APP_DIR/deploy/web-control.sh backup-now" \
    >/etc/systemd/system/ai-council-backup.service
  printf '%s\n' "[Unit]" "Description=AI Council: резервная копия каждую ночь" "" \
    "[Timer]" "OnCalendar=*-*-* 03:30:00" "RandomizedDelaySec=20min" "Persistent=true" "" \
    "[Install]" "WantedBy=timers.target" >/etc/systemd/system/ai-council-backup.timer
  systemctl daemon-reload
  if systemctl enable --now ai-council-backup.timer >/dev/null 2>&1; then
    echo "== Ночные резервные копии включены (папка $BACKUPS)"
  fi
  return 0
}

do_restart() {
  echo "== Перезапуск — $(now)"
  compose up -d --force-recreate && echo "== Готово"
}

run_action() {  # run_action update|restart  (вывод — в журнал)
  local action="$1" rc=0
  write_status running "$action" "выполняется"
  : >"$LOG"; chmod 644 "$LOG" 2>/dev/null || true
  case "$action" in
    update)  do_update >>"$LOG" 2>&1 || rc=$? ;;
    restart) do_restart >>"$LOG" 2>&1 || rc=$? ;;
    backup)  do_backup >>"$LOG" 2>&1 || rc=$? ;;
    *) echo "неизвестная заявка: $action" >>"$LOG"; rc=2 ;;
  esac
  if ((rc == 0)); then
    write_status "done" "$action" "$(tail -n1 "$LOG")"
  else
    write_status failed "$action" "$(grep -E '^!!' "$LOG" | tail -n1 || tail -n1 "$LOG")"
  fi
  return "$rc"
}

cmd_run() {
  install_backup_timer >/dev/null 2>&1 || true   # после обновления — ночные копии включаются при первой заявке
  [[ -f "$CONTROL/request" ]] || exit 0
  local action
  action="$(tr -cd 'a-z\n' <"$CONTROL/request" | head -n1)"
  rm -f "$CONTROL/request"
  run_action "${action:-update}"
}

cmd_install() {
  [[ $EUID -eq 0 ]] || { echo "Запустите с sudo: sudo bash $0 install" >&2; exit 1; }
  fix_permissions
  write_version
  [[ -f "$CONTROL/status.json" ]] || write_status idle "" ""
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    cat >/etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=AI Council: заявки из веб-интерфейса (обновление, перезапуск)

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash $APP_DIR/deploy/web-control.sh run
EOF
    cat >/etc/systemd/system/$UNIT.path <<EOF
[Unit]
Description=AI Council: следить за заявками из веб-интерфейса

[Path]
PathExists=$CONTROL/request
Unit=$UNIT.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$UNIT.path" >/dev/null
    echo "✓ Служба $UNIT включена: обновлять и перезапускать программу можно из веб-интерфейса (Настройки → Программа)."
  else
    echo "! systemd не найден: настройки из веба работают, а обновление — только командой: sudo bash $0 update-now" >&2
  fi
  install_backup_timer
  # помощник ai-council update тоже обновляет с сохранением ваших настроек
  local helper=/usr/local/bin/ai-council
  if [[ -f "$helper" ]] && ! grep -q 'web-control.sh update-now' "$helper"; then
    sed -i "s|^  update) .*|  update)  bash \"$APP_DIR/deploy/web-control.sh\" update-now ;;|" "$helper"
  fi
  # пересоздать контейнеры, чтобы веб получил доступ на запись к настройкам
  compose up -d >/dev/null 2>&1 || true
}

cmd_backup_now() {  # ночная копия (таймер) и из терминала: свой журнал, статус кнопок сайта не трогает
  mkdir -p "$CONTROL"
  local rc=0
  do_backup >>"$BACKUP_LOG" 2>&1 || rc=$?
  tail -n 200 "$BACKUP_LOG" >"$BACKUP_LOG.tmp" 2>/dev/null && mv -f "$BACKUP_LOG.tmp" "$BACKUP_LOG"
  chmod 644 "$BACKUP_LOG" 2>/dev/null || true
  tail -n 2 "$BACKUP_LOG"
  return "$rc"
}

cmd_update_now() {
  if run_action update; then tail -n 3 "$LOG"; echo "Обновлено."; else cat "$LOG"; exit 1; fi
}

case "${1:-}" in
  install)    cmd_install ;;
  run)        cmd_run ;;
  update-now) cmd_update_now ;;
  version)    write_version ;;
  backup-now) cmd_backup_now ;;
  restore)    [[ -n "${2:-}" ]] || { echo "Укажите файл: restore backups/ai-council-ДАТА.tgz" >&2; exit 2; }
              do_restore "$2" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
