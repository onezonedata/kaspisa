#!/bin/bash
# Приёмка режима установки: Т22, Т23, Т24 (local, tunnel), Т25, Т26, Т54 (public), Т55, Т63, Т64.
# Запуск: S9_SCRATCH=<папка> bash deploy/check/profile.sh local|tunnel|public
# .env готовит common.sh (проект, порты, подсеть), дальше работает настоящий install.sh.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=common.sh
source ./common.sh

mode=${1:?режим: local, tunnel или public}
case "$mode" in
local) s9_prepare local local 13000 172.29.247.0/29 ;;
tunnel) s9_prepare tunnel tunnel 13001 172.29.247.8/29 ;;
public) s9_prepare public public 13002 172.29.247.16/29 18080 18443 ;;
*)
    echo "неизвестный режим: $mode" >&2
    exit 2
    ;;
esac
trap s9_cleanup EXIT

st() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
web="http://127.0.0.1:$S9_WEB_PORT"
pass=$(s9_rand_hex | head -c 24)
json() { printf '{"email":"%s","password":"%s"}' "$1" "$pass"; }
post_register() { # ПОЧТА [curl-аргументы...]: тело ответа не печатается, код — в stdout
    local email=$1 url=$2
    shift 2
    curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-Requested-With: kaspisa' \
        -H 'Content-Type: application/json' -d "$(json "$email")" "$@" "$url"
}
sql() { s9_compose exec -T db psql -U kaspisa -d kaspisa -Atc "$1"; }
cid() { s9_compose ps -aq "$1"; }
inspect() { docker inspect -f "$1" "$(cid "$2")"; }

s9_say "== install.sh ($S9_PROJECT)"
# .env подготовила проверка (проект, порты, подсеть): install.sh видит его и идёт по шагам
# сборка, сверка подсети, up --wait. Создание .env с нуля проверяют pt-тесты со стабом docker.
s9_install >"$S9_SCRATCH/install-$S9_NAME.log" 2>&1 || {
    s9_bad "install.sh (журнал: $S9_SCRATCH/install-$S9_NAME.log)"
    exit 1
}
s9_ok "install.sh прошёл (migrate завершился, остальные healthy)"
if s9_leaks "$S9_SCRATCH/install-$S9_NAME.log"; then s9_bad "Т40: секрет в выводе install.sh"; else s9_ok "Т40: вывод install.sh без секретов"; fi

s9_eq "migrate завершился с кодом 0" 0 "$(inspect '{{.State.ExitCode}}' migrate)"
for svc in db redis api worker web backup; do
    s9_eq "$svc работает" running "$(inspect '{{.State.Status}}' "$svc")"
done
for svc in db redis api; do
    if inspect '{{json .NetworkSettings.Ports}}' "$svc" | grep -q HostPort; then
        s9_bad "$svc опубликовал порт"
    else
        s9_ok "$svc без опубликованных портов"
    fi
done
s9_eq "worker: KASPISA_DRY_RUN" true "$(s9_compose exec -T worker printenv KASPISA_DRY_RUN | tr -d '\r\n')"

# Т22: сервис backup делает первую суточную копию сразу после старта (ждём до 90 с)
daily=""
for _ in $(seq 1 45); do
    daily=$(find "$S9_DIR/backups" -maxdepth 1 -name 'daily-*.dump' | head -n 1)
    [ -n "$daily" ] && break
    sleep 2
done
if [ -n "$daily" ]; then
    s9_ok "Т22: есть суточная копия"
    s9_eq "Т22: права копии 600" 600 "$(stat_mode "$daily")"
    s9_eq "Т22: владелец копии — UID из .env" "$(s9_env KASPISA_UID)" "$(stat_uid "$daily")"
else
    s9_bad "Т22: суточной копии нет через 90 с"
fi

# Т63: состав сети edge и её подсеть
members=$(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "${S9_PROJECT}_edge" |
    tr ' ' '\n' | sed -e '/^$/d' -e "s/^${S9_PROJECT}-//" -e 's/-1$//' | sort | tr '\n' ' ')
if [ "$mode" = public ]; then
    s9_eq "Т63: в edge только api и caddy" "api caddy " "$members"
else
    s9_eq "Т63: в edge только api" "api " "$members"
fi
s9_eq "Т63: подсеть edge из .env" "$S9_SUBNET" \
    "$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "${S9_PROJECT}_edge")"

if [ "$mode" != public ]; then
    s9_eq "caddy не запущен" "" "$(s9_compose ps -q caddy)"
    s9_eq "Т22/Т55: GET /login" 200 "$(st "$web/login")"
    s9_eq "Т55: GET /register" 200 "$(st "$web/register")"
    email="owner-$mode@example.test"
    s9_eq "Т22/Т55: POST /api/auth/register через web" 201 \
        "$(post_register "$email" "$web/api/auth/register" -D "$S9_SCRATCH/h-$mode.txt" -c "$S9_SCRATCH/jar-$mode.txt")"
    if grep -i '^set-cookie:' "$S9_SCRATCH/h-$mode.txt" | grep -qi 'secure'; then
        s9_bad "Set-Cookie с Secure в режиме $mode"
    else
        s9_ok "Set-Cookie без Secure"
    fi
    s9_eq "GET /api/auth/me с cookie" 200 "$(st -b "$S9_SCRATCH/jar-$mode.txt" "$web/api/auth/me")"
    rm -f "$S9_SCRATCH/h-$mode.txt" "$S9_SCRATCH/jar-$mode.txt"

    # Т23: подделанный X-Forwarded-For через web не доходит до приложения
    # web не видит Redis и Postgres (разные сети), но api достаёт.
    for target in redis:6379 db:5432; do
        if s9_compose exec -T web node -e "require('net').connect({host:'${target%%:*}',port:${target##*:}},()=>process.exit(0)).on('error',()=>process.exit(1))"; then
            s9_bad "web достаёт до $target"
        else
            s9_ok "web не достаёт до $target"
        fi
    done
    probe="p$(s9_rand_hex | head -c 12)"
    st -H 'X-Forwarded-For: 1.2.3.4' "$web/api/auth/me?probe=$probe" >/dev/null
    web_ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"${S9_PROJECT}_app\").IPAddress}}" "$(cid web)")
    line=$(s9_compose logs --no-log-prefix api | grep "$probe" | head -n 1)
    if [[ "$line" == *"$web_ip"* ]]; then s9_ok "Т23: в журнале api адрес web"; else s9_bad "Т23: нет адреса web в строке журнала"; fi
    if [[ "$line" == *"1.2.3.4"* ]]; then s9_bad "Т23: в журнале api подделанный адрес"; else s9_ok "Т23: подделанный адрес не попал в журнал"; fi
fi

if [ "$mode" = public ]; then
    base="https://kaspisa.test:$S9_HTTPS_PORT"
    cu() { curl -sk --resolve "kaspisa.test:$S9_HTTPS_PORT:127.0.0.1" --path-as-is "$@"; }
    cst() { cu -o /dev/null -w '%{http_code}' "$@"; }

    # Т25
    s9_eq "Т25: GET /login через Caddy" 200 "$(cst "$base/login")"
    hdr=$(cu -D - -o "$S9_SCRATCH/body-me.txt" "$base/api/auth/me")
    s9_eq "Т25: /api/auth/me без сессии" 401 "$(printf '%s' "$hdr" | head -n 1 | cut -d' ' -f2)"
    if grep -q '"code"' "$S9_SCRATCH/body-me.txt"; then s9_ok "Т25: JSON с code"; else s9_bad "Т25: нет JSON с code"; fi
    rm -f "$S9_SCRATCH/body-me.txt"
    if printf '%s' "$hdr" | grep -qi '^cache-control:.*no-store'; then s9_ok "Т25: no-store"; else s9_bad "Т25: нет no-store"; fi
    if printf '%s' "$hdr" | grep -qi '^strict-transport-security:'; then s9_ok "Т25: HSTS"; else s9_bad "Т25: нет HSTS"; fi
    if s9_compose logs --no-log-prefix caddy | grep -qiE 'acme|letsencrypt'; then
        s9_bad "Т25: в журнале Caddy есть acme или letsencrypt"
    else
        s9_ok "Т25: журнал Caddy без acme и letsencrypt"
    fi

    # Т54: регистрация снаружи закрыта
    before=$(sql 'select count(*) from app_user')
    n=0
    for path in /api/auth/register /api/auth/register/ '/api/auth/register?x=1' /API/auth/register \
        /api/auth/REGISTER //api/auth/register /api//auth/register /api/auth/./register \
        /api/auth/%72egister /api/auth%2Fregister /%61pi/auth/register /api/auth/%2572egister \
        /api/auth/register%20 '/api/auth/register;x=1' /api/auth/register%2F; do
        n=$((n + 1))
        code=$(post_register "closed$n@example.test" "$base$path" -k --resolve "kaspisa.test:$S9_HTTPS_PORT:127.0.0.1" --path-as-is)
        if [ "$code" = 201 ]; then s9_bad "Т54: $path -> 201"; else s9_ok "Т54: $path -> $code"; fi
        if [ "$path" = /api/auth/register ]; then s9_eq "Т54: /api/auth/register ровно 404" 404 "$code"; fi
    done
    s9_eq "Т54: GET /register -> 404" 404 "$(cst "$base/register")"
    s9_eq "Т54: число пользователей не изменилось" "$before" "$(sql 'select count(*) from app_user')"
    email=owner-public@example.test
    s9_eq "Т54: регистрация через петлю web" 201 "$(post_register "$email" "$web/api/auth/register")"
    code=$(curl -sk --resolve "kaspisa.test:$S9_HTTPS_PORT:127.0.0.1" -o /dev/null -D "$S9_SCRATCH/h-pub.txt" \
        -c "$S9_SCRATCH/jar-pub.txt" -w '%{http_code}' -X POST -H 'X-Requested-With: kaspisa' \
        -H 'Content-Type: application/json' -d "$(json "$email")" "$base/api/auth/login")
    s9_eq "Т54: вход через Caddy" 200 "$code"
    if grep -i '^set-cookie:' "$S9_SCRATCH/h-pub.txt" | grep -qi 'secure'; then s9_ok "Т54: Set-Cookie с Secure"; else s9_bad "Т54: Set-Cookie без Secure"; fi
    s9_eq "Т54: /api/auth/me через Caddy с cookie" 200 "$(cst -b "$S9_SCRATCH/jar-pub.txt" "$base/api/auth/me")"
    rm -f "$S9_SCRATCH/h-pub.txt" "$S9_SCRATCH/jar-pub.txt"

    # Т26: адрес клиента берётся у Caddy, подделка XFF не проходит
    probe="p$(s9_rand_hex | head -c 12)"
    cst -H 'X-Forwarded-For: 1.2.3.4' "$base/api/auth/me?probe=$probe" >/dev/null
    caddy_ip=$(s9_compose logs --no-log-prefix caddy | grep "$probe" | head -n 1 |
        sed -n 's/.*"remote_ip":"\([^"]*\)".*/\1/p')
    api_ip=$(s9_compose logs --no-log-prefix api | grep "$probe" | head -n 1 |
        sed -n 's/^INFO: *\([0-9a-fA-F.:]*\):[0-9]* - .*/\1/p')
    if [ -n "$caddy_ip" ] && [ "$caddy_ip" = "$api_ip" ]; then s9_ok "Т26: адрес в журнале api = remote_ip Caddy"; else s9_bad "Т26: адреса не совпали (caddy='$caddy_ip', api='$api_ip')"; fi
    if [ "$api_ip" = 1.2.3.4 ]; then s9_bad "Т26: подделанный адрес принят"; else s9_ok "Т26: подделка XFF не прошла"; fi
    probe="p$(s9_rand_hex | head -c 12)"
    s9_compose exec -T web node -e "fetch('http://api:8000/api/auth/me?probe=$probe',{headers:{'X-Forwarded-For':'1.2.3.4'}}).then(()=>{})" || true
    web_ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"${S9_PROJECT}_app\").IPAddress}}" "$(cid web)")
    api_ip=$(s9_compose logs --no-log-prefix api | grep "$probe" | head -n 1 |
        sed -n 's/^INFO: *\([0-9a-fA-F.:]*\):[0-9]* - .*/\1/p')
    s9_eq "Т26: прямой запрос из web — в журнале адрес web" "$web_ip" "$api_ip"
fi

# Т64: расхождение подсети (только в local)
if [ "$mode" = local ]; then
    s9_say "== Т64: расхождение подсети edge"
    s9_compose stop >/dev/null 2>&1
    {
        grep -v '^KASPISA_EDGE_SUBNET=' "$S9_DIR/.env"
        echo "KASPISA_EDGE_SUBNET=172.29.247.24/29"
    } >"$S9_DIR/.env.new"
    mv "$S9_DIR/.env.new" "$S9_DIR/.env"
    chmod 600 "$S9_DIR/.env"
    sum_before=$(shasum "$S9_DIR/.env")
    if err=$(s9_install 2>&1); then rc=0; else rc=$?; fi
    s9_eq "Т64: install.sh вернул 1" 1 "$rc"
    if [[ "$err" == *"docker compose down"* ]]; then s9_ok "Т64: в сообщении docker compose down"; else s9_bad "Т64: нет подсказки docker compose down"; fi
    s9_eq "Т64: api, worker, web остановлены" "" "$(s9_compose ps -q --status running api worker web)"
    s9_eq "Т64: .env не изменён" "$sum_before" "$(shasum "$S9_DIR/.env")"
    s9_compose down >/dev/null 2>&1
    if s9_install >"$S9_SCRATCH/install-$S9_NAME-2.log" 2>&1; then
        s9_ok "Т64: после down install.sh проходит"
    else
        s9_bad "Т64: после down подъём не прошёл"
    fi
    s9_eq "Т64: подсеть edge новая" 172.29.247.24/29 \
        "$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "${S9_PROJECT}_edge")"
fi

# Т40 (часть): журналы сервисов не содержат ключа и паролей
s9_compose logs --no-log-prefix >"$S9_SCRATCH/logs-$S9_NAME.txt" 2>&1 || true
if s9_leaks "$S9_SCRATCH/logs-$S9_NAME.txt"; then s9_bad "Т40: секрет в журналах сервисов"; else s9_ok "Т40: секретов в журналах нет"; fi
rm -f "$S9_SCRATCH/logs-$S9_NAME.txt"

s9_finish
