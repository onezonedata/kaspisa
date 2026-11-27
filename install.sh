#!/bin/bash
# Установка kaspisa: создаёт .env (права 600) с ключом шифрования и паролями базы, собирает или
# скачивает образы и поднимает сервисы. Секретов не печатает: ключ показывает `make show-key`.
#   bash install.sh [local|tunnel|public]
# Для public нужны KASPISA_DOMAIN и KASPISA_CADDY_TLS (e-mail для Let's Encrypt или internal):
# из окружения или вопросом в терминале. Повторный запуск .env не трогает.
# Переменные KASPISA_* из оболочки в compose не попадают: значения берутся только из .env.
# Только для проверок: COMPOSE_PROJECT_NAME, KASPISA_WEB_PORT, KASPISA_EDGE_SUBNET,
# KASPISA_HTTP_PORT, KASPISA_HTTPS_PORT из окружения попадают в .env.
set -euo pipefail
umask 077
# Трассировка (set -x) запрещена: она напечатала бы секреты.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

TMP_ENV=""
cleanup() {
    if [ -n "$TMP_ENV" ]; then rm -f "$TMP_ENV"; fi
    kaspisa_lock_release
}
trap cleanup EXIT

die() {
    echo "install.sh: $*" >&2
    exit 1
}

# Блокировка берётся до любого вызова docker.
kaspisa_lock_acquire "$ROOT" install.sh || exit 1

if ! docker compose version >/dev/null 2>&1; then
    die "не найден docker compose. На Ubuntu/Debian поставьте Docker: sudo bash deploy/prepare-server.sh"
fi
# `compose version` проходит и без доступа к демону: проверяем его до создания .env.
if ! docker info >/dev/null 2>&1; then
    die "нет доступа к демону docker. Добавьте себя в группу docker (sudo usermod -aG docker \$USER, затем войдите заново) и повторите; через sudo запускать не нужно: копии получили бы владельца root"
fi

# В public закрытие регистрации держится на порте web только на 127.0.0.1. В Docker Engine до 28.0
# соседи по сегменту сети могли достучаться до такого порта (moby #45610), поэтому нужна версия 28+.
# Версию не удалось прочитать — предупреждаем и продолжаем.
check_docker_version() {
    local mode=$1 ver major
    [ "$mode" = public ] || return 0
    ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)
    major=${ver%%.*}
    if [[ $major =~ ^[0-9]+$ ]]; then
        if [ "$major" -lt 28 ]; then
            die "для режима public нужен Docker Engine 28 или новее (сейчас $ver): обновите Docker"
        fi
    else
        echo "install.sh: не удалось определить версию Docker; для режима public нужна 28 или новее." >&2
    fi
}

interactive=""
if [ -t 0 ]; then interactive=1; fi
requested=${1:-}

if [ -f .env ]; then
    mode=$(kaspisa_env_get .env COMPOSE_PROFILES)
    if [ -n "$requested" ] && [ "$requested" != "$mode" ]; then
        die "режим уже выбран: $mode (в .env). Чтобы сменить, измените COMPOSE_PROFILES вручную"
    fi
    check_docker_version "$mode"
    echo ".env уже существует, режим $mode: секреты не меняются."
else
    mode=$requested
    if [ -z "$mode" ]; then
        [ -n "$interactive" ] || die "укажите режим: bash install.sh local|tunnel|public"
        read -r -p "Режим установки (local, tunnel, public) [tunnel]: " mode
        mode=${mode:-tunnel}
    fi
    case "$mode" in
    local | tunnel | public) ;;
    *) die "неизвестный режим: $mode (нужен local, tunnel или public)" ;;
    esac
    check_docker_version "$mode"
    domain=""
    tls=""
    if [ "$mode" = public ]; then
        domain=${KASPISA_DOMAIN:-}
        tls=${KASPISA_CADDY_TLS:-}
        if [ -z "$domain" ] && [ -n "$interactive" ]; then read -r -p "Домен (например, kaspisa.example.com): " domain; fi
        if [ -z "$tls" ] && [ -n "$interactive" ]; then read -r -p "E-mail для Let's Encrypt (или internal): " tls; fi
        [ -n "$domain" ] && [ -n "$tls" ] || die "для public нужны KASPISA_DOMAIN и KASPISA_CADDY_TLS"
    fi

    # Секреты живут только в переменных оболочки и попадают в файл перенаправлением.
    key="1:$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
    pg_password=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    owner_password=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    app_password=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    version=$(git describe --tags --always 2>/dev/null || echo dev)
    cookie_secure=false
    if [ "$mode" = public ]; then cookie_secure=true; fi

    TMP_ENV=$ROOT/.env.tmp.$$
    {
        if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then printf 'COMPOSE_PROJECT_NAME=%s\n' "$COMPOSE_PROJECT_NAME"; fi
        printf 'COMPOSE_PROFILES=%s\n' "$mode"
        printf 'KASPISA_VERSION=%s\n' "$version"
        printf 'KASPISA_IMAGE_PREFIX=\n'
        printf 'KASPISA_DRY_RUN=true\n'
        printf 'KASPISA_COOKIE_SECURE=%s\n' "$cookie_secure"
        printf 'KASPISA_ENCRYPTION_KEY=%s\n' "$key"
        printf 'KASPISA_ENCRYPTION_OLD_KEYS=\n'
        printf 'POSTGRES_PASSWORD=%s\n' "$pg_password"
        printf 'KASPISA_DB_OWNER_PASSWORD=%s\n' "$owner_password"
        printf 'KASPISA_DB_APP_PASSWORD=%s\n' "$app_password"
        printf 'KASPISA_WEB_PORT=%s\n' "${KASPISA_WEB_PORT:-3000}"
        printf 'KASPISA_UID=%s\n' "$(id -u)"
        printf 'KASPISA_GID=%s\n' "$(id -g)"
        printf 'KASPISA_UPDATE_REPO=onezonedata/kaspisa\n'
        printf 'KASPISA_EDGE_SUBNET=%s\n' "${KASPISA_EDGE_SUBNET:-172.29.247.0/29}"
        if [ -n "${KASPISA_HTTP_PORT:-}" ]; then printf 'KASPISA_HTTP_PORT=%s\n' "$KASPISA_HTTP_PORT"; fi
        if [ -n "${KASPISA_HTTPS_PORT:-}" ]; then printf 'KASPISA_HTTPS_PORT=%s\n' "$KASPISA_HTTPS_PORT"; fi
        if [ "$mode" = public ]; then
            printf 'KASPISA_DOMAIN=%s\n' "$domain"
            printf 'KASPISA_CADDY_TLS=%s\n' "$tls"
        fi
    } >"$TMP_ENV"
    chmod 600 "$TMP_ENV"
    mv "$TMP_ENV" .env
    TMP_ENV=""
    unset key pg_password owner_password app_password
    echo "Создан .env (права 600), режим $mode."
fi

prefix=$(kaspisa_env_get .env KASPISA_IMAGE_PREFIX)
if [ -z "$prefix" ]; then
    echo "Собираю образы (первый раз это несколько минут)..."
    kaspisa_compose build
else
    kaspisa_compose pull --policy missing
fi
kaspisa_check_edge_subnet .env || exit 1
echo "Запускаю сервисы..."
kaspisa_compose up -d --no-build --wait --wait-timeout 300

port=$(kaspisa_env_get .env KASPISA_WEB_PORT)
port=${port:-3000}
echo
echo "kaspisa установлена."
case "$mode" in
local)
    echo "Откройте http://localhost:$port и зарегистрируйтесь."
    ;;
tunnel)
    echo "Интерфейс слушает только 127.0.0.1:$port на этом сервере. Два способа открыть его:"
    echo "  ssh -L $port:127.0.0.1:$port <сервер>   затем http://localhost:$port"
    echo "  tailscale serve --bg $port              затем адрес *.ts.net из вывода команды"
    ;;
public)
    domain=$(kaspisa_env_get .env KASPISA_DOMAIN)
    echo "Интерфейс: https://$domain"
    echo "Регистрация снаружи закрыта. Первую учётную запись заведите через петлю сервера:"
    echo "  ssh -L 3000:127.0.0.1:$port <сервер>   затем http://localhost:3000/register"
    ;;
esac
echo
echo "Сохраните ключ шифрования отдельно от копий (менеджер паролей): в своём терминале выполните"
echo "  make show-key"
echo "Без ключа копия базы не вернёт доступы к магазинам."
