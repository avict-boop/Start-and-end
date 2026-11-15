#!/bin/sh
# Запуск программы в контейнере. Стартует от root только затем, чтобы вернуть права на папку данных пользователю
# council (uid 1000): после переноса архива (docker compose cp) или восстановления копии файлы оказываются у root,
# и программа не могла бы сохранять заседания, отчёты и настройки. Дальше — от council, без прав root.
set -e
if [ "$(id -u)" = "0" ]; then
  # только то, что принадлежит не council: на большом архиве это быстро
  find /app/runs ! -user 1000 -exec chown 1000:1000 {} + 2>/dev/null || true
  for p in /app/config.yaml /app/control /app/branding; do
    [ -e "$p" ] && chown -R 1000:1000 "$p" 2>/dev/null || true   # том только для чтения — пропускается
  done
  exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi
exec "$@"
