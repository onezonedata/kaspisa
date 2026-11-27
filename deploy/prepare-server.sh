#!/bin/bash
# Подготовка чистого сервера Ubuntu/Debian: Docker Engine с плагином compose, ufw, по флагу — Tailscale.
#   sudo bash deploy/prepare-server.sh [--public] [--tailscale]
# --public    открыть 80 и 443 (режим public, Caddy)
# --tailscale поставить Tailscale; `tailscale up` владелец запускает сам (вход через браузер)
# Порты SSH для ufw берутся из `sshd -T` (не только 22). Повторный запуск безопасен. SSH по ключу, смена порта, fail2ban и автообновления — этап 15.
set -euo pipefail

PUBLIC=0
TAILSCALE=0
for arg in "$@"; do
    case "$arg" in
        --public) PUBLIC=1 ;;
        --tailscale) TAILSCALE=1 ;;
        *)
            echo "неизвестный аргумент: $arg (допустимо: --public, --tailscale)" >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "запустите от root или через sudo" >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "нет /etc/os-release: поддерживаются только Ubuntu и Debian" >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
    ubuntu | debian) DISTRO=$ID ;;
    *)
        echo "поддерживаются только Ubuntu и Debian, найдено: ${ID:-?}" >&2
        exit 1
        ;;
esac
CODENAME=${VERSION_CODENAME:-}
if [ -z "$CODENAME" ]; then
    echo "не удалось определить кодовое имя выпуска ОС" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
ARCH=$(dpkg --print-architecture)

# 1. Docker Engine и плагин compose из официального репозитория Docker.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "Docker уже установлен, шаг пропущен"
else
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$DISTRO/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$DISTRO $CODENAME stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
fi

# 2. ufw: SSH разрешается ДО включения, иначе можно потерять доступ.
if ! command -v ufw >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ufw
fi
ufw default deny incoming
ufw default allow outgoing
# Порты берём у работающего sshd, а не предполагаем 22: иначе после `ufw enable` новое
# подключение по нестандартному порту невозможно (останется консоль хостера).
# Источники (объединяются, лишний порт безвреден): `port` и `listenaddress хост:порт` из sshd -T
# и сокет systemd ssh.socket (Ubuntu 24.04: там порт задаёт он, а не sshd_config).
SSH_PORTS=$(
    {
        sshd -T 2>/dev/null | awk '/^port /{print $2} /^listenaddress /{n=split($2,a,":"); print a[n]}'
        systemctl show ssh.socket -p Listen 2>/dev/null | awk -F'[=: ]' '/^Listen=/{print $(NF-1)}'
    } | grep -E '^[0-9]+$' | sort -un || true
)
if [ -z "$SSH_PORTS" ]; then
    echo "не удалось определить порт sshd (sshd -T, ssh.socket); ufw не включён, чтобы не потерять доступ" >&2
    exit 1
fi
for ssh_port in $SSH_PORTS; do
    ufw allow "$ssh_port/tcp"
done
if [ "$PUBLIC" -eq 1 ]; then
    ufw allow 80/tcp
    ufw allow 443/tcp
fi
ufw --force enable
echo "Внимание: порты, опубликованные Docker, ufw не фильтрует; защита — в docker-compose.prod.yml."

# 3. Tailscale (по флагу): вход владелец выполняет сам.
if [ "$TAILSCALE" -eq 1 ]; then
    if command -v tailscale >/dev/null 2>&1; then
        echo "Tailscale уже установлен, шаг пропущен"
    else
        apt-get update
        apt-get install -y curl gnupg
        install -m 0755 -d /usr/share/keyrings
        curl -fsSL "https://pkgs.tailscale.com/stable/$DISTRO/$CODENAME.noarmor.gpg" \
            -o /usr/share/keyrings/tailscale-archive-keyring.gpg
        curl -fsSL "https://pkgs.tailscale.com/stable/$DISTRO/$CODENAME.tailscale-keyring.list" \
            -o /etc/apt/sources.list.d/tailscale.list
        apt-get update
        apt-get install -y tailscale
    fi
    echo "Войдите в Tailscale сами (откроется ссылка для браузера): sudo tailscale up"
    echo "Затем откройте интерфейс по tailnet: tailscale serve --bg 3000"
fi

echo "Сервер подготовлен. Дальше: bash install.sh [local|tunnel|public]"
