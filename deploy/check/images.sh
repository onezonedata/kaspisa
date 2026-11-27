#!/bin/bash
# Приёмка образов, Т19: сборка каждой платформы по отдельности и проверки содержимого.
# Запуск: bash deploy/check/images.sh [linux/amd64 linux/arm64]
# Образы получают временные теги kaspisa-s9/…:s9-<arch> и в конце удаляются; ничего не публикуется.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=common.sh
source ./common.sh

platforms=("$@")
[ ${#platforms[@]} -gt 0 ] || platforms=(linux/amd64 linux/arm64)

run() { # ОБРАЗ ПЛАТФОРМА КОМАНДА...
    local image=$1 platform=$2
    shift 2
    docker run --rm --platform "$platform" --entrypoint "$1" "$image" "${@:2}"
}

for platform in "${platforms[@]}"; do
    arch=${platform#linux/}
    for img in backend web; do
        ctx=$S9_ROOT/backend
        [ "$img" = web ] && ctx=$S9_ROOT/frontend
        tag=kaspisa-s9/kaspisa-$img:s9-$arch
        s9_say "== $img $platform"
        if docker buildx build --platform "$platform" --load -t "$tag" "$ctx" >/dev/null 2>&1; then
            s9_ok "сборка $img $platform"
        else
            s9_bad "сборка $img $platform"
            continue
        fi
        uid=$(run "$tag" "$platform" id -u | tr -d '\r\n')
        if [ "$uid" != 0 ] && [ -n "$uid" ]; then s9_ok "$img: не root (UID $uid)"; else s9_bad "$img: работает от root"; fi
        found=$(run "$tag" "$platform" find / -name '.env*' -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null || true)
        s9_eq "$img: файлов .env* нет" "" "$found"
        if [ "$img" = backend ]; then
            if run "$tag" "$platform" test -d /app/tests; then s9_bad "backend: есть каталог tests"; else s9_ok "backend: каталога tests нет"; fi
            if run "$tag" "$platform" python -c 'import kaspisa.main' >/dev/null 2>&1; then s9_ok "backend: import kaspisa.main"; else s9_bad "backend: import kaspisa.main"; fi
            if run "$tag" "$platform" alembic -c alembic/alembic.ini heads >/dev/null 2>&1; then s9_ok "backend: alembic видит ревизии"; else s9_bad "backend: alembic не запускается"; fi
        else
            if run "$tag" "$platform" node -e 1; then s9_ok "web: node -e 1"; else s9_bad "web: node -e 1"; fi
            if run "$tag" "$platform" test -f /app/server.js; then s9_ok "web: server.js на месте"; else s9_bad "web: нет server.js"; fi
        fi
    done
done

for platform in "${platforms[@]}"; do
    for img in backend web; do
        docker image rm "kaspisa-s9/kaspisa-$img:s9-${platform#linux/}" >/dev/null 2>&1 || true
    done
done
s9_finish
