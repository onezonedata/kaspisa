#!/bin/bash
# Общие функции скриптов установки (подключается через `source`): вызов compose, чтение .env,
# блокировка backups/.lock.d, сверка подсети edge.
# shellcheck shell=bash

# Вызов compose с файлом prod. Скрипты запускаются из корня репозитория: .env подхватывается сам.
# Без -f compose взял бы docker-compose.yml разработки.
#
# Compose отдаёт переменной окружения приоритет над .env, поэтому KASPISA_*, POSTGRES_* и
# COMPOSE_PROFILES/COMPOSE_FILE из оболочки владельца (например, KASPISA_DRY_RUN=false или ключ
# разработки) молча заменили бы значения из .env. Они снимаются на вызов; остаётся
# COMPOSE_PROJECT_NAME (имя проекта, не секрет; его задают проверки). Намеренные подстановки
# скрипт передаёт массивом KASPISA_COMPOSE_SET=(ИМЯ=значение ...), объявленным как local в вызывающей
# функции (так работают compose_new* в update.sh).
kaspisa_compose() {
    local name unset_args=()
    while IFS= read -r name; do
        case "$name" in
        KASPISA_* | POSTGRES_* | COMPOSE_PROFILES | COMPOSE_FILE | COMPOSE_ENV_FILES)
            unset_args+=(-u "$name")
            ;;
        esac
    done < <(compgen -e)
    env ${unset_args[@]+"${unset_args[@]}"} ${KASPISA_COMPOSE_SET[@]+"${KASPISA_COMPOSE_SET[@]}"} \
        docker compose -f docker-compose.prod.yml "$@"
}

# Блокировка на хосте: `mkdir` атомарен и есть и в Linux, и в macOS (flock в macOS нет).
# kaspisa_lock_acquire КОРЕНЬ ИМЯ_СКРИПТА. Занята — сообщение в stderr и код 1.
KASPISA_LOCK_HELD=""
kaspisa_lock_acquire() {
    local root=$1 name=$2 dir pid other
    dir=$root/backups/.lock.d
    # Режим нужен только самому backups (глубочайший каталог); родитель — корень проекта.
    # shellcheck disable=SC2174
    mkdir -p -m 700 "$root/backups"
    if mkdir "$dir" 2>/dev/null; then
        printf '%s %s\n' "$$" "$name" >"$dir/pid"
        KASPISA_LOCK_HELD=$dir
        return 0
    fi
    pid="?" other="?"
    if [ -r "$dir/pid" ]; then read -r pid other <"$dir/pid" || true; fi
    echo "уже выполняется $other (pid $pid); если процесса нет — удалите backups/.lock.d" >&2
    return 1
}

# Снимает только свою блокировку: чужая (код 1 выше) остаётся на месте.
kaspisa_lock_release() {
    if [ -n "$KASPISA_LOCK_HELD" ]; then rm -rf "$KASPISA_LOCK_HELD"; fi
    KASPISA_LOCK_HELD=""
}

# Значение переменной из .env: kaspisa_env_get ФАЙЛ ИМЯ. Последняя строка побеждает, кавычек нет.
kaspisa_env_get() {
    local line
    line=$(grep -E "^$2=" "$1" | tail -n 1) || true
    printf '%s' "${line#*=}"
}

# Сверка подсети уже созданной сети edge с KASPISA_EDGE_SUBNET из .env (ADR 0023, возражение 6).
# Сети нет — проверка пропускается. Расхождение — сообщение в stderr и код 1 до любого `up`.
kaspisa_check_edge_subnet() {
    local env_file=$1 project want nets net have
    project=${COMPOSE_PROJECT_NAME:-$(kaspisa_env_get "$env_file" COMPOSE_PROJECT_NAME)}
    project=${project:-kaspisa-prod}
    want=$(kaspisa_env_get "$env_file" KASPISA_EDGE_SUBNET)
    want=${want:-172.29.247.0/29}
    nets=$(docker network ls -q \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.network=edge")
    [ -n "$nets" ] || return 0
    for net in $nets; do
        have=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$net")
        have=${have% }
        if [ "$have" != "$want" ]; then
            {
                echo "Подсеть сети edge ($have) не совпадает с KASPISA_EDGE_SUBNET в .env ($want)."
                echo "Доверие к адресу клиента держится на этой подсети. Пересоздайте сеть:"
                echo "  docker compose down   # тома с данными не удаляются"
                echo "и повторите команду."
            } >&2
            return 1
        fi
    done
}
