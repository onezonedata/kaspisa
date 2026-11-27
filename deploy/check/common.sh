#!/bin/bash
# Общие функции приёмочных проверок дистрибутива (не входят в `make test`).
# Проверка идёт на чистой копии рабочего дерева в своём проекте compose (kaspisa-s9-<имя>),
# со своими портами и подсетью edge. Общие контейнеры разработки не трогаются.
# Секреты: в вывод не попадают; `docker compose config` и `docker inspect` с окружением не вызываются.
# shellcheck shell=bash

S9_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
S9_SCRATCH=${S9_SCRATCH:-${TMPDIR:-/tmp}/kaspisa-s9}
S9_FAILS=0

s9_say() { printf '%s\n' "$*"; }
s9_ok() { printf 'OK    %s\n' "$*"; }
s9_bad() {
    printf 'FAIL  %s\n' "$*"
    S9_FAILS=$((S9_FAILS + 1))
}

# s9_assert ОПИСАНИЕ КОМАНДА...: команда должна вернуть 0.
s9_assert() {
    local what=$1
    shift
    if "$@" >/dev/null 2>&1; then s9_ok "$what"; else s9_bad "$what"; fi
}

# s9_eq ОПИСАНИЕ ОЖИДАЕМОЕ ФАКТИЧЕСКОЕ
s9_eq() {
    if [ "$2" = "$3" ]; then s9_ok "$1"; else s9_bad "$1 (ожидалось '$2', получено '$3')"; fi
}

s9_rand_hex() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# s9_prepare ИМЯ РЕЖИМ ВЕБ_ПОРТ ПОДСЕТЬ [HTTP_ПОРТ HTTPS_ПОРТ]
# Чистая копия (файлы из git, включая ещё не закоммиченные) и .env с правами 600.
s9_prepare() {
    S9_NAME=$1
    S9_MODE=$2
    S9_WEB_PORT=$3
    S9_SUBNET=$4
    S9_HTTP_PORT=${5:-18080}
    S9_HTTPS_PORT=${6:-18443}
    S9_PROJECT=kaspisa-s9-$S9_NAME
    S9_DIR=$S9_SCRATCH/s9-$S9_NAME
    rm -rf "$S9_DIR"
    mkdir -p "$S9_DIR"
    (cd "$S9_ROOT" && git ls-files -z -co --exclude-standard | tar --null -T - -cf -) |
        tar -xf - -C "$S9_DIR"
    (
        umask 077
        {
            echo "COMPOSE_PROJECT_NAME=$S9_PROJECT"
            echo "COMPOSE_PROFILES=$S9_MODE"
            echo "KASPISA_VERSION=s9check"
            echo "KASPISA_IMAGE_PREFIX="
            echo "KASPISA_DRY_RUN=true"
            if [ "$S9_MODE" = public ]; then
                echo "KASPISA_COOKIE_SECURE=true"
            else
                echo "KASPISA_COOKIE_SECURE=false"
            fi
            echo "KASPISA_ENCRYPTION_KEY=1:$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
            echo "KASPISA_ENCRYPTION_OLD_KEYS="
            echo "POSTGRES_PASSWORD=$(s9_rand_hex)"
            echo "KASPISA_DB_OWNER_PASSWORD=$(s9_rand_hex)"
            echo "KASPISA_DB_APP_PASSWORD=$(s9_rand_hex)"
            echo "KASPISA_WEB_PORT=$S9_WEB_PORT"
            echo "KASPISA_UID=$(id -u)"
            echo "KASPISA_GID=$(id -g)"
            echo "KASPISA_UPDATE_REPO="
            echo "KASPISA_EDGE_SUBNET=$S9_SUBNET"
            echo "KASPISA_HTTP_PORT=$S9_HTTP_PORT"
            echo "KASPISA_HTTPS_PORT=$S9_HTTPS_PORT"
            if [ "$S9_MODE" = public ]; then
                echo "KASPISA_DOMAIN=kaspisa.test"
                echo "KASPISA_CADDY_TLS=internal"
            fi
        } >"$S9_DIR/.env"
    )
    # shellcheck disable=SC2174
    mkdir -p -m 700 "$S9_DIR/backups"
}

# Все вызовы compose идут из каталога копии: .env подхватывается сам.
s9_compose() { (cd "$S9_DIR" && docker compose -f docker-compose.prod.yml "$@"); }

# Настоящий install.sh в каталоге копии (там же .env, подготовленный s9_prepare).
s9_install() { (cd "$S9_DIR" && bash install.sh "$S9_MODE"); }

# Права и владелец файла (macOS и Linux различаются ключами stat).
stat_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
stat_uid() { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }

s9_services() {
    # migrate в списке нет: запускается через depends_on, а завершившийся одноразовый сервис,
    # названный в `up --wait` явно, compose считает ошибкой (Р0).
    local list="db redis api worker web backup"
    [ "$S9_MODE" = public ] && list="$list caddy"
    echo "$list"
}

s9_up() {
    # shellcheck disable=SC2046
    s9_compose up -d --no-build --wait --wait-timeout 300 $(s9_services)
}

s9_build() { s9_compose build >"$S9_SCRATCH/build-$S9_NAME.log" 2>&1; }

s9_cleanup() {
    if [ -n "${S9_DIR:-}" ] && [ -d "$S9_DIR" ]; then
        if [ -z "${S9_KEEP:-}" ]; then
            s9_compose down -v --remove-orphans >/dev/null 2>&1 || true
            rm -rf "$S9_DIR"
        else
            s9_say "S9_KEEP: проект $S9_PROJECT и каталог $S9_DIR оставлены"
        fi
    fi
}

# Значение из .env копии (для паролей: только для проверок, в вывод не печатается).
s9_env() { grep -E "^$1=" "$S9_DIR/.env" | tail -n 1 | cut -d= -f2-; }

# Подстрока секрета в тексте: код 0, если нашлась. Совпадения не печатаются.
s9_leaks() {
    local file=$1 name value
    for name in KASPISA_ENCRYPTION_KEY POSTGRES_PASSWORD KASPISA_DB_OWNER_PASSWORD KASPISA_DB_APP_PASSWORD; do
        value=$(s9_env "$name")
        value=${value#1:}
        if grep -qF -- "$value" "$file"; then return 0; fi
    done
    return 1
}

s9_finish() {
    if [ "$S9_FAILS" -eq 0 ]; then
        s9_say "ИТОГ: все проверки прошли"
        return 0
    fi
    s9_say "ИТОГ: провалено проверок: $S9_FAILS"
    return 1
}
