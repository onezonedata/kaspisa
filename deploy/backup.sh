#!/bin/bash
# Копии базы и восстановление (ADR 0023). Работает в контейнере `backup` (образ postgres:17):
# подключение — переменные PGHOST, PGUSER, PGPASSWORD, PGDATABASE; пароль в аргументы не попадает.
#   backup.sh loop                  раз в час запускает daily-if-due отдельным процессом
#   backup.sh daily-if-due          суточная копия, если свежей (младше 24 ч) нет
#   backup.sh manual                ручная копия
#   backup.sh update                копия перед обновлением (update.sh), своя ротация
#   backup.sh dump КЛАСС            копия без блокировки (её берут команды выше)
#   backup.sh restore /backups/ИМЯ  восстановление (вызывает deploy/restore.sh)
# Скрипт вызывается как `bash backup.sh`: бит исполнения на хосте не гарантирован.
# Каталог копий — KASPISA_BACKUP_DIR (умолчание /backups).
set -euo pipefail
umask 077

DIR=${KASPISA_BACKUP_DIR:-/backups}
LOCK=$DIR/.lock
SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
HERE=$(dirname "$SELF")
STAMP='[0-9]{8}T[0-9]{6}Z'

# Сколько копий каждого класса хранить (Н7).
keep_for() {
    case "$1" in
    daily) echo 7 ;;
    manual) echo 3 ;;
    update) echo 3 ;; # копии update.sh: свой класс, чтобы не вытеснять ручные копии владельца
    *)
        echo "backup.sh: неизвестный класс копии: $1" >&2
        exit 2
        ;;
    esac
}

# Имена файлов класса, точно совпадающие с шаблоном, от старых к новым.
list_class() {
    local class=$1 f name
    for f in "$DIR/$class"-*.dump; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        if [[ $name =~ ^$class-$STAMP\.dump$ ]]; then echo "$name"; fi
    done | sort
}

rotate() {
    local class=$1 keep count
    keep=$(keep_for "$class")
    count=$(list_class "$class" | wc -l)
    if [ "$count" -gt "$keep" ]; then
        list_class "$class" | head -n "$((count - keep))" | while read -r name; do
            rm -f -- "$DIR/$name"
        done
    fi
}

dump() {
    local class=${1:-}
    keep_for "$class" >/dev/null
    local ts target tmp
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    target=$DIR/$class-$ts.dump
    # Две копии в одну секунду получили бы одно имя: ждём следующую секунду.
    while [ -e "$target" ]; do
        sleep 1
        ts=$(date -u +%Y%m%dT%H%M%SZ)
        target=$DIR/$class-$ts.dump
    done
    tmp=$DIR/.$class-$ts.dump.tmp
    if ! pg_dump -Fc >"$tmp"; then
        rm -f -- "$tmp"
        echo "backup.sh: pg_dump завершился с ошибкой, копия не создана" >&2
        exit 1
    fi
    mv -- "$tmp" "$target"
    rotate "$class"
    echo "backup.sh: создана копия $(basename "$target")"
}

# Время из имени YYYYmmddTHHMMSSZ в секундах эпохи на чистом bash (`date -d` в macOS и Debian разный).
stamp_epoch() {
    local s=$1 y m d hh mm ss era yoe doy doe days
    y=$((10#${s:0:4})) m=$((10#${s:4:2})) d=$((10#${s:6:2}))
    hh=$((10#${s:9:2})) mm=$((10#${s:11:2})) ss=$((10#${s:13:2}))
    if [ "$m" -le 2 ]; then y=$((y - 1)); fi
    era=$((y / 400))
    yoe=$((y - era * 400))
    doy=$(((153 * ((m + 9) % 12) + 2) / 5 + d - 1))
    doe=$((yoe * 365 + yoe / 4 - yoe / 100 + doy))
    days=$((era * 146097 + doe - 719468))
    echo $((days * 86400 + hh * 3600 + mm * 60 + ss))
}

daily_locked() {
    local last name now
    last=$(list_class daily | tail -n 1)
    if [ -n "$last" ]; then
        name=${last#daily-}
        name=${name%.dump}
        now=$(date -u +%s)
        if [ $((now - $(stamp_epoch "$name"))) -lt 86400 ]; then return 0; fi
    fi
    dump daily
}

# Блокировка в контейнере: суточная копия не снимается посреди восстановления и наоборот.
daily_if_due() {
    local rc=0
    flock -n -E 75 "$LOCK" bash "$SELF" _daily_locked || rc=$?
    if [ "$rc" -eq 75 ]; then return 0; fi
    return "$rc"
}

valid_backup_path() {
    [[ $1 =~ ^$DIR/(daily|manual|update)-$STAMP\.dump$ ]]
}

restore_locked() {
    local file=$1 db=${PGDATABASE:-kaspisa}
    [ -f "$file" ] || {
        echo "backup.sh: файла копии нет: $file" >&2
        exit 1
    }
    # Схема удаляется целиком, чтобы не остались объекты, которых нет в копии (например, таблица
    # несостоявшейся миграции). Права роли приложения возвращает grants.sql.
    psql -X -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public'
    pg_restore --no-owner --exit-on-error -d "$db" "$file"
    psql -X -v ON_ERROR_STOP=1 -q -f "$HERE/postgres/grants.sql"
    echo "backup.sh: база восстановлена из $(basename "$file")"
}

loop() {
    trap 'exit 0' TERM INT
    while true; do
        bash "$SELF" daily-if-due || echo "backup.sh: сбой суточной копии, повтор через час" >&2
        # sleep в фоне: bash (PID 1 контейнера) обрабатывает TERM сразу, а не после сна.
        sleep 3600 &
        wait $! || true
    done
}

case "${1:-}" in
loop) loop ;;
daily-if-due) daily_if_due ;;
_daily_locked) daily_locked ;;
manual) flock -w 600 "$LOCK" bash "$SELF" dump manual ;;
update) flock -w 600 "$LOCK" bash "$SELF" dump update ;;
dump) dump "${2:-}" ;;
restore)
    file=${2:-}
    if ! valid_backup_path "$file"; then
        echo "backup.sh: имя копии должно быть $DIR/(daily|manual|update)-<метка>.dump" >&2
        exit 2
    fi
    flock -w 600 "$LOCK" bash "$SELF" _restore_locked "$file"
    ;;
_restore_locked) restore_locked "${2:?}" ;;
*)
    echo "использование: backup.sh loop|daily-if-due|manual|dump КЛАСС|restore ПУТЬ" >&2
    exit 2
    ;;
esac
