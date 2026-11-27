#!/bin/sh
# Запуск API в контейнере. Флаги uvicorn выбираются по режиму установки (KASPISA_MODE),
# потому что профиль compose не может сменить команду сервиса (ADR 0023).
#   local, tunnel: адресу из X-Forwarded-For не верим.
#   public: верим только адресам из подсети edge, где живут Caddy и api.
set -eu

case "${KASPISA_MODE:-}" in
local | tunnel)
    exec uvicorn kaspisa.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
    ;;
public)
    if [ -z "${KASPISA_EDGE_SUBNET:-}" ]; then
        echo "start-api: в режиме public нужна KASPISA_EDGE_SUBNET" >&2
        exit 2
    fi
    exec uvicorn kaspisa.main:app --host 0.0.0.0 --port 8000 \
        --proxy-headers --forwarded-allow-ips="$KASPISA_EDGE_SUBNET"
    ;;
*)
    echo "start-api: KASPISA_MODE должен быть local, tunnel или public" >&2
    exit 2
    ;;
esac
