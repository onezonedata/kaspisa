#!/bin/bash
# Восстановление базы из копии: bash deploy/restore.sh [--yes] [--db-only] backups/ИМЯ.dump
# (или `make restore FILE=backups/ИМЯ.dump`). Останавливает api и worker, заменяет схему public
# данными копии (в контейнере backup, от владельца базы), возвращает права роли приложения
# и запускает api и worker: перед ними `migrate` поднимет копию старой версии до текущей схемы.
#   --yes      без вопроса (для проверок)
#   --db-only  только база: сервисы не останавливаются и не запускаются, блокировку держит
#              вызывающий (его использует update.sh)
# Ключ шифрования в копии нет: на новом сервере сначала замените KASPISA_ENCRYPTION_KEY в .env.
set -euo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
# shellcheck source=lib.sh
source "$ROOT/deploy/lib.sh"
trap kaspisa_lock_release EXIT

assume_yes=""
db_only=""
file=""
while [ $# -gt 0 ]; do
    case "$1" in
    --yes) assume_yes=1 ;;
    --db-only) db_only=1 ;;
    -*)
        echo "restore.sh: неизвестный флаг $1" >&2
        exit 2
        ;;
    *) file=$1 ;;
    esac
    shift
done

# Блокировка берётся до любой другой работы.
if [ -z "$db_only" ]; then kaspisa_lock_acquire "$ROOT" restore.sh || exit 1; fi

if [ -z "$file" ]; then
    echo "restore.sh: укажите копию: backups/ИМЯ.dump" >&2
    exit 2
fi
dir=$(cd "$(dirname "$file")" 2>/dev/null && pwd -P) || dir=""
name=$(basename "$file")
if [ "$dir" != "$ROOT/backups" ] || ! [[ $name =~ ^(daily|manual|update)-[0-9]{8}T[0-9]{6}Z\.dump$ ]]; then
    echo "restore.sh: копия должна лежать в backups/ и называться daily-, manual- или update-<метка>.dump" >&2
    exit 1
fi
if [ ! -f "$ROOT/backups/$name" ]; then
    echo "restore.sh: файла нет: backups/$name" >&2
    exit 1
fi

if [ -z "$db_only" ] && [ -z "$assume_yes" ]; then
    [ -t 0 ] || {
        echo "restore.sh: нужен терминал для подтверждения (или флаг --yes)" >&2
        exit 1
    }
    echo "База будет заменена данными из backups/$name. Всё, что записано после копии, пропадёт."
    read -r -p "Введите yes, чтобы продолжить: " answer
    [ "$answer" = yes ] || {
        echo "Отменено."
        exit 1
    }
fi

if [ -z "$db_only" ]; then kaspisa_compose stop api worker; fi

if ! kaspisa_compose exec -T backup bash /opt/kaspisa/deploy/backup.sh restore "/backups/$name"; then
    {
        echo "restore.sh: восстановление не удалось, база в неизвестном состоянии."
        if [ -z "$db_only" ]; then echo "api и worker остановлены. Повторите: bash deploy/restore.sh backups/$name"; fi
    } >&2
    exit 1
fi

if [ -z "$db_only" ]; then
    kaspisa_compose up -d --no-build --wait --wait-timeout 300 api worker
    echo "Восстановлено из backups/$name, api и worker запущены."
fi
