#!/bin/bash
# Приёмка update.sh: Т41–Т47, Т56, Т58–Т60, Т65, Т40 (часть). Занимает 20–40 минут, внешняя сеть
# и GHCR не нужны: «новые версии» — локально собранные или перемеченные образы и локальный
# репозиторий U («верхний»); установки I (реестр) и B (сборка) — его клоны.
# Запуск: S9_SCRATCH=<папка> bash deploy/check/update.sh
# Для режима сборки (Т46, Т47) нужны `make setup` и базовые образы; без сети задайте
#   UV_OFFLINE=1 npm_config_offline=true
# Порты тестовой базы режима сборки: KASPISA_PG_PORT (15432) и KASPISA_REDIS_PORT (16379).
# Секреты в вывод не попадают: только OK/FAIL, коды и имена файлов.
# S9_NAME и S9_PROJECT читает common.sh (глобальные переменные проекта); `ls -t` нужен для порядка
# по времени изменения журналов.
# shellcheck disable=SC2034,SC2012
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# shellcheck source=common.sh
source ./common.sh
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=s9 GIT_AUTHOR_EMAIL=s9@example.test
export GIT_COMMITTER_NAME=s9 GIT_COMMITTER_EMAIL=s9@example.test
export KASPISA_PG_PORT=${KASPISA_PG_PORT:-15432} KASPISA_REDIS_PORT=${KASPISA_REDIS_PORT:-16379}

PREFIX=localhost:1/kaspisa
SC=$S9_SCRATCH
U=$SC/s9-up-upper
BUILD=$SC/s9-up-build
WRAP=$SC/s9-up-wrap
CTL=$SC/s9-up-ctl
LOGS=$SC/s9-up-logs
REAL_DOCKER=$(command -v docker)
DIR_I="" DIR_B=""
TAGS=()

use() { # i или b: переключает глобальные переменные common.sh
    if [ "$1" = i ]; then
        S9_DIR=$DIR_I S9_NAME=up S9_PROJECT=kaspisa-s9-up PORT=13020
    else
        S9_DIR=$DIR_B S9_NAME=ub S9_PROJECT=kaspisa-s9-ub PORT=13021
    fi
}

cleanup_all() {
    local n
    for n in i b; do
        use "$n"
        [ -d "${S9_DIR:-/nonexistent}" ] || continue
        if [ -z "${S9_KEEP:-}" ]; then
            docker compose -p "$S9_PROJECT-update-test" -f "$S9_DIR/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
            s9_compose down -v --remove-orphans >/dev/null 2>&1 || true
        fi
    done
    if [ -z "${S9_KEEP:-}" ]; then
        local t
        for t in ${TAGS[@]+"${TAGS[@]}"}; do docker rmi "$t" >/dev/null 2>&1 || true; done
        rm -rf "$U" "$BUILD" "$WRAP" "$CTL" "$DIR_I" "$DIR_B" "$LOGS"
    else
        s9_say "S9_KEEP: проекты и папки оставлены ($SC/s9-up-*)"
    fi
}
trap cleanup_all EXIT

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
env_set() { # ИМЯ ЗНАЧЕНИЕ: строка в .env текущей установки
    local tmp=$S9_DIR/.env.new
    (
        umask 077
        grep -v "^$1=" "$S9_DIR/.env" >"$tmp" || true
        printf '%s=%s\n' "$1" "$2" >>"$tmp"
    )
    mv "$tmp" "$S9_DIR/.env"
}
g() { git -C "$S9_DIR" "$@"; }
u() { git -C "$U" "$@"; }
sql() { s9_compose exec -T db psql -U kaspisa -d kaspisa -Atc "$1" 2>/dev/null; }
rev() { sql 'SELECT version_num FROM alembic_version'; }
cid() { s9_compose ps -q "$1"; }
started() { docker inspect -f '{{.State.StartedAt}}' "$(cid "$1")"; }
image_of() { docker inspect -f '{{.Config.Image}}' "$(cid "$1")"; }
health_of() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(cid "$1")"; }
# Копии update.sh — класс update (ручные manual-* ротацией обновлений не затрагиваются).
manuals() { find "$S9_DIR/backups" -maxdepth 1 -name 'update-*.dump' | sort; }
newest_manual() { manuals | tail -n 1; }
# Состояние, которое откат обязан вернуть: HEAD, .env, alembic_version.
core() { printf '%s %s %s' "$(g rev-parse HEAD)" "$(sha "$S9_DIR/.env")" "$(rev)"; }
# Ещё и время запуска: для отказов до остановки контейнеров.
full() { printf '%s %s %s %s' "$(core)" "$(started api)" "$(started worker)" "$(started web)"; }
json() { printf '{"email":"%s","password":"%s"}' "$1" "$PASS"; }
api_post() { # ПУТЬ ПОЧТА → код ответа
    curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-Requested-With: kaspisa' \
        -H 'Content-Type: application/json' -d "$(json "$2")" "http://127.0.0.1:$PORT$1"
}
rotate_check() { s9_compose exec -T api python -m kaspisa.db.rotate_secrets --check >/dev/null 2>&1 && echo 0 || echo $?; }

RUN=0
# run_update ИМЯ АРГУМЕНТЫ...: update.sh в текущей установке; код — в UPD_RC, вывод — в UPD_OUT.
# PATH с обёрткой docker задаёт вызывающий через WRAPPED=1.
run_update() {
    local name=$1
    shift
    RUN=$((RUN + 1))
    UPD_OUT=$LOGS/update-$RUN-$name.out
    : >"$CTL/calls.log"
    rm -f "$CTL/sent"
    UPD_RC=0
    (cd "$S9_DIR" && PATH="$WRAP:$PATH" W_CTL="$CTL" bash update.sh "$@") >"$UPD_OUT" 2>&1 || UPD_RC=$?
}
set_mode() { printf '%s' "$1" >"$CTL/mode"; }

assert_old_running() { # ТЕГ: api, worker, web на образах :ТЕГ, healthy
    local t=$1
    s9_eq "$2: api на :$t" "$PREFIX/kaspisa-backend:$t" "$(image_of api)"
    s9_eq "$2: worker на :$t" "$PREFIX/kaspisa-backend:$t" "$(image_of worker)"
    s9_eq "$2: web на :$t" "$PREFIX/kaspisa-web:$t" "$(image_of web)"
    s9_eq "$2: api healthy" healthy "$(health_of api)"
    s9_eq "$2: web healthy" healthy "$(health_of web)"
    s9_eq "$2: worker running" running "$(health_of worker)"
}
no_lock() { s9_assert "$1: блокировка снята" test ! -e "$S9_DIR/backups/.lock.d"; }
show_tail() { # при провале — хвост вывода update.sh (журнал без секретов проверяется отдельно)
    s9_say "  --- $UPD_OUT (хвост) ---"
    tail -n 25 "$UPD_OUT" | sed 's/^/  | /'
}

# ---- образы и репозиторий U
build_backend() { # ТЕГ: сборка образа backend из клона U на этой метке; web — перемечается
    local t=$1
    rm -rf "$BUILD"
    git clone -q "$U" "$BUILD" && git -C "$BUILD" checkout -q "$t" || return 1
    (
        umask 077
        cp "$S9_DIR/.env" "$BUILD/.env"
    )
    (cd "$BUILD" && KASPISA_IMAGE_PREFIX=$PREFIX KASPISA_VERSION=$t docker compose -f docker-compose.prod.yml build api) \
        >"$LOGS/build-$t.log" 2>&1 || {
        s9_bad "сборка образа backend $t (журнал $LOGS/build-$t.log)"
        return 1
    }
    docker tag "$PREFIX/kaspisa-web:v0.1.0" "$PREFIX/kaspisa-web:$t"
    TAGS+=("$PREFIX/kaspisa-backend:$t" "$PREFIX/kaspisa-web:$t")
}
retag() { # ТЕГ: образы той же сборки, что v0.1.0
    docker tag "$PREFIX/kaspisa-backend:v0.1.0" "$PREFIX/kaspisa-backend:$1"
    docker tag "$PREFIX/kaspisa-web:v0.1.0" "$PREFIX/kaspisa-web:$1"
    TAGS+=("$PREFIX/kaspisa-backend:$1" "$PREFIX/kaspisa-web:$1")
}
u_branch() { u checkout -q -B "rel-$1" "$2"; } # НОВАЯ_МЕТКА ОТ_ЧЕГО
u_release() {                                  # МЕТКА СООБЩЕНИЕ
    u add -A
    u commit -q -m "$2"
    u tag "$1"
    u checkout -q main
}
write_migration() { # ИМЯ_ТАБЛИЦЫ [fail]
    local extra=""
    if [ "${2:-}" = fail ]; then extra='    op.execute("SELECT 1/0")'; fi
    cat >"$U/backend/alembic/versions/0008_s9_$1.py" <<EOF
"""Проверочная ревизия приёмки update.sh."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TABLE $1 (id integer PRIMARY KEY)")
$extra


def downgrade() -> None:
    op.execute("DROP TABLE $1")
EOF
}

# clone_install ИМЯ РЕЖИМ_ПОРТ ПОДСЕТЬ: установка — клон U с .env из s9_prepare
clone_install() {
    s9_prepare "$1" local "$2" "$3"
    local tmp=$SC/s9-up-env-$1
    mv "$S9_DIR/.env" "$tmp"
    rm -rf "$S9_DIR"
    git clone -q "$U" "$S9_DIR"
    mv "$tmp" "$S9_DIR/.env"
    mkdir -m 700 "$S9_DIR/backups"
}

mkdir -p "$LOGS" "$WRAP" "$CTL"
cat >"$WRAP/docker" <<EOF
#!/bin/bash
# Обёртка docker для приёмки: журнал вызовов up и сигнал update.sh после первого успешного stop.
ctl=\${W_CTL:-}
mode=\$(cat "\$ctl/mode" 2>/dev/null || echo none)
if [ "\${1:-}" = inspect ] && [ "\$mode" = inspectfail ]; then
    echo "обёртка: docker inspect отказывает" >&2
    exit 1
fi
if [ "\${1:-}" = compose ] && [[ " \$* " == *" up "* ]]; then
    printf 'up|%s|DRY=%s\n' "\$*" "\${KASPISA_DRY_RUN-unset}" >>"\$ctl/calls.log"
fi
"$REAL_DOCKER" "\$@"
rc=\$?
if [ "\$rc" = 0 ] && [ "\${1:-}" = compose ] && [[ " \$* " == *" stop "* ]] && [ ! -e "\$ctl/sent" ]; then
    case "\$mode" in
    term | pty) touch "\$ctl/sent"; kill -TERM "\$(cat "\$ctl/pid")" ;;
    hup) touch "\$ctl/sent"; kill -HUP "\$(cat "\$ctl/pid")" ;;
    esac
fi
exit \$rc
EOF
chmod 755 "$WRAP/docker"
set_mode none

# ---- U: рабочее дерево, метка v0.1.0 и все проверяемые версии
rm -rf "$U"
mkdir -p "$U"
(cd "$S9_ROOT" && git ls-files -z -co --exclude-standard | tar --null -T - -cf -) | tar -xf - -C "$U"
mkdir -p "$U/docs"
{
    echo "# Инструкция (заглушка приёмки)"
    echo
    echo "Строка X: исходный текст."
    for i in 1 2 3 4 5 6; do echo "Строка $i."; done
} >"$U/docs/deploy.md"
u init -q -b main
u add -A
u commit -q -m "v0.1.0"
u tag v0.1.0

s9_say "== подготовка: клон I, образы v0.1.0, установка"
clone_install up 13020 172.29.247.96/29
DIR_I=$S9_DIR
use i
env_set KASPISA_IMAGE_PREFIX "$PREFIX"
env_set KASPISA_VERSION v0.1.0
# web v0.1.0: образ проверки Ч3 (frontend с тех пор не менялся) или сборка.
if ! docker image inspect "$PREFIX/kaspisa-web:v0.1.0" >/dev/null 2>&1; then
    if docker image inspect local/kaspisa-web:s9check >/dev/null 2>&1; then
        docker tag local/kaspisa-web:s9check "$PREFIX/kaspisa-web:v0.1.0"
    else
        (cd "$DIR_I" && KASPISA_VERSION=v0.1.0 docker compose -f docker-compose.prod.yml build web) >"$LOGS/build-web.log" 2>&1 ||
            {
                s9_bad "сборка web"
                exit 1
            }
    fi
fi
TAGS+=("$PREFIX/kaspisa-web:v0.1.0")
build_backend v0.1.0 || exit 1

# v0.1.1 (Т41): в начало update.sh 30 строк комментария — смещения меняются
u_branch v0.1.1 v0.1.0
{
    head -n 1 "$U/update.sh"
    for i in $(seq 30); do echo "# s9: строка $i для смещения"; done
    tail -n +2 "$U/update.sh"
} >"$U/update.sh.new"
mv "$U/update.sh.new" "$U/update.sh"
u_release v0.1.1 "v0.1.1"
retag v0.1.1
# v0.1.2 (Т42): метка есть, образов нет
u_branch v0.1.2 v0.1.0
echo "Строка v0.1.2." >>"$U/docs/deploy.md"
u_release v0.1.2 "v0.1.2"
# v0.1.3 (Т44): успешная миграция и api, который сразу падает
u_branch v0.1.3 v0.1.0
write_migration s9_probe
printf '#!/bin/sh\nexit 1\n' >"$U/backend/docker/start-api.sh"
u_release v0.1.3 "v0.1.3"
build_backend v0.1.3 || exit 1
# v0.1.4 (Т58): падающая миграция
u_branch v0.1.4 v0.1.0
write_migration s9_fail fail
u_release v0.1.4 "v0.1.4"
build_backend v0.1.4 || exit 1
# v0.1.5 (Т60): на основе v0.1.1, backup.sh печатает метку в stderr
u_branch v0.1.5 v0.1.1
{
    sed 's|^manual) flock|manual) echo s9-backup-v2 >\&2; flock|' "$U/deploy/backup.sh"
} >"$U/deploy/backup.sh.new"
mv "$U/deploy/backup.sh.new" "$U/deploy/backup.sh"
grep -q 's9-backup-v2' "$U/deploy/backup.sh" || s9_bad "подготовка v0.1.5: backup.sh не изменён"
u_release v0.1.5 "v0.1.5"
retag v0.1.5
# v0.1.6 (Т65): успешная ревизия 0008
u_branch v0.1.6 v0.1.5
write_migration s9_probe
u_release v0.1.6 "v0.1.6"
build_backend v0.1.6 || exit 1
# Режим сборки (Т45–Т47)
u_branch m-conflict v0.1.0
{
    sed '3s/.*/Строка X: правка из U./' "$U/docs/deploy.md"
} >"$U/docs/deploy.md.new"
mv "$U/docs/deploy.md.new" "$U/docs/deploy.md"
u_release m-conflict "m-conflict"
u_branch m-lint v0.1.0
printf 'import os\n' >"$U/backend/src/kaspisa/s9_lint_bad.py"
u_release m-lint "m-lint"
u_branch m-docs v0.1.0
echo "Строка m-docs." >>"$U/docs/deploy.md"
u_release m-docs "m-docs"

PASS=$(s9_rand_hex | head -c 24)
s9_say "== установка I (v0.1.0, режим реестра)"
use i
s9_install >"$LOGS/install-i.log" 2>&1 || {
    s9_bad "install.sh в I (журнал $LOGS/install-i.log)"
    exit 1
}
s9_eq "A: регистрация" 201 "$(api_post /api/auth/register a@example.test)"
s9_eq "A: вход" 200 "$(api_post /api/auth/login a@example.test)"
s9_compose exec -T backup bash /opt/kaspisa/deploy/backup.sh manual >/dev/null 2>&1 # заранее есть копия
OWNER_COPY=$(find "$S9_DIR/backups" -maxdepth 1 -name 'manual-*.dump' | sort | head -n 1)

# ---- Т43: грязное дерево
s9_say "== Т43: грязное дерево"
before=$(full)
echo "# правка владельца" >>"$S9_DIR/Makefile"
run_update dirty v0.1.1
s9_eq "Т43: код 1" 1 "$UPD_RC"
g checkout -q -- Makefile
s9_eq "Т43: HEAD, .env, контейнеры без изменений" "$before" "$(full)"
no_lock "Т43"

# ---- Т42: образов нет
s9_say "== Т42: отсутствующий образ"
before=$(full)
m_before=$(manuals)
run_update noimage v0.1.2
s9_eq "Т42: код 1" 1 "$UPD_RC"
s9_eq "Т42: HEAD, .env, контейнеры без изменений" "$before" "$(full)"
s9_eq "Т42: новых копий нет" "$m_before" "$(manuals)"
no_lock "Т42"

# ---- Т58: падающая миграция
s9_say "== Т58: падающая миграция"
before=$(core)
m_before=$(newest_manual)
run_update migfail v0.1.4
s9_eq "Т58: код 1" 1 "$UPD_RC"
s9_eq "Т58: HEAD, .env, alembic_version прежние" "$before" "$(core)"
s9_eq "Т58: таблицы s9_fail нет" "" "$(sql "SELECT to_regclass('public.s9_fail')")"
if grep -q 'не восстанавливалась' "$UPD_OUT"; then s9_ok "Т58: в выводе: база не восстанавливалась"; else s9_bad "Т58: нет сообщения о невосстановлении"; show_tail; fi
if [ "$(newest_manual)" != "$m_before" ]; then s9_ok "Т58: копия снималась"; else s9_bad "Т58: новой копии нет"; fi
s9_eq "Т58: вход A" 200 "$(api_post /api/auth/login a@example.test)"
assert_old_running v0.1.0 "Т58"
no_lock "Т58"

# ---- Т44: сбой после успешной миграции
s9_say "== Т44: сбой после миграции"
before=$(core)
m_before=$(newest_manual)
run_update afterfail v0.1.3
s9_eq "Т44: код 1" 1 "$UPD_RC"
s9_eq "Т44: HEAD, .env, alembic_version прежние" "$before" "$(core)"
s9_eq "Т44: таблицы s9_probe нет" "" "$(sql "SELECT to_regclass('public.s9_probe')")"
s9_eq "Т44: вход A" 200 "$(api_post /api/auth/login a@example.test)"
s9_eq "Т44: регистрация нового" 201 "$(api_post /api/auth/register n@example.test)"
assert_old_running v0.1.0 "Т44"
if grep -Eq 'update-[0-9]{8}T[0-9]{6}Z\.dump' "$UPD_OUT" && grep -q 'База восстановлена' "$UPD_OUT" &&
    [ "$(newest_manual)" != "$m_before" ]; then
    s9_ok "Т44: в сообщении имя копии и «база восстановлена»"
else
    s9_bad "Т44: нет имени копии или «база восстановлена»"
    show_tail
fi
no_lock "Т44"

# ---- Т56 (а): сигнал TERM после остановки
s9_say "== Т56а: TERM"
before=$(core)
m_before=$(manuals)
set_mode term
(cd "$S9_DIR" && exec env PATH="$WRAP:$PATH" W_CTL="$CTL" bash update.sh v0.1.1) >"$LOGS/update-term.out" 2>&1 &
printf '%s' $! >"$CTL/pid"
UPD_RC=0
wait $! || UPD_RC=$?
UPD_OUT=$LOGS/update-term.out
set_mode none
s9_eq "Т56а: код 1" 1 "$UPD_RC"
s9_eq "Т56а: HEAD, .env, alembic_version прежние" "$before" "$(core)"
s9_eq "Т56а: новых копий нет" "$m_before" "$(manuals)"
assert_old_running v0.1.0 "Т56а"
no_lock "Т56а"

# ---- Т56 (в): непредусмотренный сбой (docker inspect) после шага 7
s9_say "== Т56в: сбой docker inspect (окно worker 60 с)"
before=$(core)
m_before=$(newest_manual)
set_mode inspectfail
run_update inspectfail v0.1.1
set_mode none
if [ "$UPD_RC" != 0 ]; then s9_ok "Т56в: код $UPD_RC (не 0)"; else s9_bad "Т56в: код 0"; fi
s9_eq "Т56в: HEAD, .env, alembic_version прежние" "$before" "$(core)"
if [ "$(newest_manual)" != "$m_before" ]; then s9_ok "Т56в: есть новая копия"; else s9_bad "Т56в: новой копии нет"; fi
if grep -q 'База восстановлена' "$UPD_OUT"; then s9_ok "Т56в: база восстановлена"; else s9_bad "Т56в: нет «база восстановлена»"; show_tail; fi
s9_eq "Т56в: вход A" 200 "$(api_post /api/auth/login a@example.test)"
assert_old_running v0.1.0 "Т56в"
no_lock "Т56в"

# ---- Т41 и Т59: успех в режиме реестра, окно DRY_RUN
s9_say "== Т41, Т59: v0.1.0 -> v0.1.1 (60 с окно worker)"
env_before=$(grep -v '^KASPISA_VERSION=' "$S9_DIR/.env")
m_before=$(newest_manual)
run_update ok v0.1.1
s9_eq "Т41: код 0" 0 "$UPD_RC"
[ "$UPD_RC" = 0 ] || show_tail
s9_eq "Т41: KASPISA_VERSION в .env" v0.1.1 "$(s9_env KASPISA_VERSION)"
s9_eq "Т41: права .env 600" 600 "$(stat_mode "$S9_DIR/.env")"
s9_eq "Т41: прочие строки .env прежние" "$env_before" "$(grep -v '^KASPISA_VERSION=' "$S9_DIR/.env")"
assert_old_running v0.1.1 "Т41"
s9_eq "Т41: HEAD = v0.1.1" "$(u rev-parse 'v0.1.1^{commit}')" "$(g rev-parse HEAD)"
if [ "$(newest_manual)" != "$m_before" ]; then s9_ok "Т41: новая копия update-*"; else s9_bad "Т41: новой копии нет"; fi
s9_eq "Т41: вход A" 200 "$(api_post /api/auth/login a@example.test)"
no_lock "Т41"
s9_eq "Т41: журнал update-*.log с итогом 0" 1 \
    "$(grep -l '^итог: код 0$' "$S9_DIR"/backups/update-*.log 2>/dev/null | wc -l | tr -d ' ')"
s9_eq "Т41: права журнала 600" 600 "$(stat_mode "$(ls -t "$S9_DIR"/backups/update-*.log | head -n 1)")"

calls=$(grep -E ' (api|worker)[ a-z]*\|DRY=' "$CTL/calls.log")
others=$(printf '%s\n' "$calls" | sed '$d')
last=$(printf '%s\n' "$calls" | tail -n 1)
s9_eq "Т59: до последнего up worker были up api web и up worker" 1 \
    "$([ "$(printf '%s\n' "$others" | grep -c .)" -ge 2 ] && echo 1 || echo 0)"
s9_eq "Т59: все up до последнего — с KASPISA_DRY_RUN=true" 0 "$(printf '%s\n' "$others" | grep -vc 'DRY=true$')"
case "$last" in
*' worker|DRY=unset') s9_ok "Т59: последний up worker — без KASPISA_DRY_RUN в окружении" ;;
*) s9_bad "Т59: последний up: $last" ;;
esac

# ---- назад на v0.1.0 той же командой (возврат к прежней метке)
s9_say "== возврат update.sh v0.1.0"
run_update back v0.1.0
s9_eq "возврат: код 0" 0 "$UPD_RC"
s9_eq "возврат: KASPISA_VERSION" v0.1.0 "$(s9_env KASPISA_VERSION)"

# ---- Т56 (б): обрыв терминала во время обновления
s9_say "== Т56б: обрыв терминала (pty), v0.1.0 -> v0.1.1"
m_before=$(newest_manual)
set_mode pty
: >"$CTL/calls.log"
rm -f "$CTL/sent"
(cd "$S9_DIR" && exec env PATH="$WRAP:$PATH" W_CTL="$CTL" script -q "$LOGS/pty.out" bash update.sh v0.1.1) >/dev/null 2>&1 &
printf '%s' $! >"$CTL/pid"
pty_pid=$!
for _ in $(seq 40); do
    [ -e "$CTL/sent" ] && break
    sleep 1
done
if [ ! -e "$CTL/sent" ]; then
    s9_bad "Т56б: обёртка не сработала (script не запустил update.sh?)"
fi
wait "$pty_pid" 2>/dev/null || true
set_mode none
done_log=""
for _ in $(seq 150); do
    done_log=$(grep -l '^итог: код' "$S9_DIR"/backups/update-*.log 2>/dev/null | xargs ls -t 2>/dev/null | head -n 1)
    if [ -n "$done_log" ] && [ "$(ls -t "$S9_DIR"/backups/update-*.log | head -n 1)" = "$done_log" ] &&
        grep -q '^итог: код 0$' "$done_log"; then break; fi
    sleep 4
done
last_log=$(ls -t "$S9_DIR"/backups/update-*.log | head -n 1)
if grep -q '^итог: код 0$' "$last_log"; then s9_ok "Т56б: в журнале итог с кодом 0"; else s9_bad "Т56б: в журнале нет итога с кодом 0"; tail -n 15 "$last_log" | sed 's/^/  | /'; fi
s9_eq "Т56б: KASPISA_VERSION v0.1.1" v0.1.1 "$(s9_env KASPISA_VERSION)"
assert_old_running v0.1.1 "Т56б"
s9_eq "Т56б: HEAD = v0.1.1" "$(u rev-parse 'v0.1.1^{commit}')" "$(g rev-parse HEAD)"
if [ "$(newest_manual)" != "$m_before" ]; then s9_ok "Т56б: новая копия"; else s9_bad "Т56б: новой копии нет"; fi
no_lock "Т56б"
pkill -f 'script -q '"$LOGS"/pty.out >/dev/null 2>&1 || true
sleep 1

# ---- Т60: скрипт копий изменился
s9_say "== Т60: backup.sh меняется между версиями (v0.1.1 -> v0.1.5)"
bk_before=$(started backup)
run_update backupv2 v0.1.5
s9_eq "Т60: код 0" 0 "$UPD_RC"
[ "$UPD_RC" = 0 ] || show_tail
bk_err=$(cd "$S9_DIR" && make backup 2>&1 >/dev/null)
case "$bk_err" in
*s9-backup-v2*) s9_ok "Т60: make backup печатает s9-backup-v2" ;;
*) s9_bad "Т60: make backup без s9-backup-v2" ;;
esac
s9_eq "Т60: контейнер backup не пересоздавался" "$bk_before" "$(started backup)"

# ---- Т65: копию старой версии поднимает до новой схемы
s9_say "== Т65: v0.1.5 -> v0.1.6 (ревизия 0008) и восстановление PRE_DUMP"
m_before=$(newest_manual)
run_update mig v0.1.6
s9_eq "Т65: код 0" 0 "$UPD_RC"
[ "$UPD_RC" = 0 ] || show_tail
pre=$(newest_manual)
if [ "$pre" != "$m_before" ] && [ -n "$pre" ]; then s9_ok "Т65: PRE_DUMP — новый update-*"; else s9_bad "Т65: PRE_DUMP не определён"; fi
s9_eq "Т65: alembic_version после обновления" 0008 "$(rev)"
s9_eq "Т65: регистрация B" 201 "$(api_post /api/auth/register b@example.test)"
(cd "$S9_DIR" && bash deploy/restore.sh --yes "backups/$(basename "$pre")") >"$LOGS/restore-i.log" 2>&1
s9_eq "Т65: restore.sh код 0" 0 "$?"
s9_eq "Т65: alembic_version = 0008 (migrate поднял копию)" 0008 "$(rev)"
s9_eq "Т65: таблица s9_probe есть" s9_probe "$(sql "SELECT to_regclass('public.s9_probe')")"
s9_eq "Т65: вход A" 200 "$(api_post /api/auth/login a@example.test)"
s9_eq "Т65: вход B — 401" 401 "$(api_post /api/auth/login b@example.test)"
s9_eq "Т65: rotate_secrets --check" 0 "$(rotate_check)"
if [ -n "$OWNER_COPY" ] && [ -f "$OWNER_COPY" ]; then
    s9_ok "Т78: ручная копия владельца пережила все обновления"
else
    s9_bad "Т78: ручная копия владельца вытеснена"
fi

# ---- Т40 (часть): секреты в журналах обновлений и сервисов I
s9_say "== Т40: секреты в журналах I"
cat "$S9_DIR"/backups/update-*.log "$LOGS"/update-*.out "$LOGS/restore-i.log" "$LOGS/install-i.log" >"$LOGS/all-i.txt"
s9_compose logs --no-log-prefix >>"$LOGS/all-i.txt" 2>&1 || true
if s9_leaks "$LOGS/all-i.txt"; then s9_bad "Т40: секрет в журналах I"; else s9_ok "Т40: журналы I без секретов"; fi

# ---- режим сборки: B
s9_say "== установка B (режим сборки)"
clone_install ub 13021 172.29.247.104/29
DIR_B=$S9_DIR
use b
docker tag "$PREFIX/kaspisa-backend:v0.1.0" local/kaspisa-backend:v0.1.0
docker tag "$PREFIX/kaspisa-web:v0.1.0" local/kaspisa-web:v0.1.0
TAGS+=(local/kaspisa-backend:v0.1.0 local/kaspisa-web:v0.1.0)
env_set KASPISA_VERSION v0.1.0
s9_install >"$LOGS/install-b.log" 2>&1 || {
    s9_bad "install.sh в B (журнал $LOGS/install-b.log)"
    exit 1
}

# Т45: конфликт слияния
s9_say "== Т45: конфликт слияния"
sed '3s/.*/Строка X: правка владельца./' "$S9_DIR/docs/deploy.md" >"$S9_DIR/docs/deploy.md.new"
mv "$S9_DIR/docs/deploy.md.new" "$S9_DIR/docs/deploy.md"
g add -A
g commit -q -m "правка владельца"
before=$(full)
run_update conflict m-conflict
s9_eq "Т45: код 1" 1 "$UPD_RC"
s9_eq "Т45: HEAD, .env, контейнеры без изменений" "$before" "$(full)"
s9_eq "Т45: git status чистый" "" "$(g status --porcelain)"
s9_assert "Т45: слияние не висит" test ! -e "$S9_DIR/.git/MERGE_HEAD"
no_lock "Т45"

# Т46: ошибка ruff
s9_say "== Т46: ошибка lint"
before=$(full)
m_before=$(manuals)
run_update lint m-lint
s9_eq "Т46: код 1" 1 "$UPD_RC"
s9_eq "Т46: HEAD, .env, alembic_version, StartedAt прежние" "$before" "$(full)"
s9_eq "Т46: новых копий нет" "$m_before" "$(manuals)"
if grep -q 'make lint не прошёл' "$UPD_OUT" &&
    grep -q 's9_lint_bad' "$(ls -t "$S9_DIR"/backups/update-*.log | head -n 1)"; then
    s9_ok "Т46: отказ из-за ошибки ruff в добавленном файле"
else
    s9_bad "Т46: отказ не из-за добавленной ошибки ruff"
    show_tail
fi
s9_eq "Т46: git status чистый" "" "$(g status --porcelain)"
no_lock "Т46"

# Т47: успех в режиме сборки
s9_say "== Т47: успех в режиме сборки (make lint и make test)"
run_update build m-docs
s9_eq "Т47: код 0" 0 "$UPD_RC"
[ "$UPD_RC" = 0 ] || show_tail
ver=$(g describe --tags --always)
s9_eq "Т47: KASPISA_VERSION = git describe" "$ver" "$(s9_env KASPISA_VERSION)"
s9_eq "Т47: api на образе версии" "local/kaspisa-backend:$ver" "$(image_of api)"
s9_eq "Т47: worker на образе версии" "local/kaspisa-backend:$ver" "$(image_of worker)"
s9_eq "Т47: web на образе версии" "local/kaspisa-web:$ver" "$(image_of web)"
TAGS+=("local/kaspisa-backend:$ver" "local/kaspisa-web:$ver")
s9_eq "Т47: слияние выполнено (есть строка m-docs)" 1 "$(grep -c 'Строка m-docs' "$S9_DIR/docs/deploy.md")"
no_lock "Т47"

# Т40 (часть): B
cat "$S9_DIR"/backups/update-*.log "$LOGS"/update-*.out "$LOGS/install-b.log" >"$LOGS/all-b.txt"
s9_compose logs --no-log-prefix >>"$LOGS/all-b.txt" 2>&1 || true
if s9_leaks "$LOGS/all-b.txt"; then s9_bad "Т40: секрет в журналах B"; else s9_ok "Т40: журналы B без секретов"; fi

s9_finish
