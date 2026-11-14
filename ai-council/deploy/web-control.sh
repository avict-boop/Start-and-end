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

set -Euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(git -C "$APP_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$APP_DIR")"
CONTROL="$APP_DIR/control"
APP_UID=1000   # пользователь внутри контейнера (см. Dockerfile)
UNIT=ai-council-control
LOG="$CONTROL/update.log"

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
  echo "== Готово"
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
  # помощник ai-council update тоже обновляет с сохранением ваших настроек
  local helper=/usr/local/bin/ai-council
  if [[ -f "$helper" ]] && ! grep -q 'web-control.sh update-now' "$helper"; then
    sed -i "s|^  update) .*|  update)  bash \"$APP_DIR/deploy/web-control.sh\" update-now ;;|" "$helper"
  fi
  # пересоздать контейнеры, чтобы веб получил доступ на запись к настройкам
  compose up -d >/dev/null 2>&1 || true
}

cmd_update_now() {
  if run_action update; then tail -n 3 "$LOG"; echo "Обновлено."; else cat "$LOG"; exit 1; fi
}

case "${1:-}" in
  install)    cmd_install ;;
  run)        cmd_run ;;
  update-now) cmd_update_now ;;
  version)    write_version ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
