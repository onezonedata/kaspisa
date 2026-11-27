#!/bin/bash
# Приёмка ролей базы: Т27 (права ролей) и Т62 (пароли не попадают в журнал при сбое init).
# Запуск: S9_SCRATCH=<папка> bash deploy/check/db-roles.sh
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=common.sh
source ./common.sh

sql_as() { # РОЛЬ ЗАПРОС: psql по локальному сокету контейнера db (в образе вход trust)
    s9_compose exec -T db psql -U "$1" -d kaspisa -Atc "$2"
}

# --- Т27 ---
s9_prepare dbroles local 13003 172.29.247.32/29
trap s9_cleanup EXIT
s9_say "== Т27: роли ($S9_PROJECT)"
s9_build || {
    s9_bad "docker compose build"
    exit 1
}
{
    s9_compose up -d --no-build --wait --wait-timeout 300 db &&
        s9_compose run --rm --no-deps migrate
} >"$S9_SCRATCH/up-dbroles.log" 2>&1 || {
    s9_bad "up db и migrate"
    exit 1
}

for role in kaspisa kaspisa_app; do
    s9_eq "Т27: $role без SUPERUSER, CREATEROLE, CREATEDB" "false|false|false" \
        "$(sql_as postgres "select rolsuper||'|'||rolcreaterole||'|'||rolcreatedb from pg_roles where rolname='$role'")"
done
tables=$(sql_as postgres "select count(*) from pg_tables where schemaname='public'")
foreign=$(sql_as postgres "select count(*) from pg_tables where schemaname='public' and tableowner<>'kaspisa'")
if [ "$tables" -gt 0 ]; then s9_ok "Т27: таблицы созданы миграциями ($tables)"; else s9_bad "Т27: таблиц нет"; fi
s9_eq "Т27: все таблицы принадлежат kaspisa" 0 "$foreign"
for stmt in "ALTER TABLE tenant DISABLE TRIGGER ALL" "DROP TABLE tenant" "CREATE TABLE s9_x()" \
    "COPY (SELECT 1) TO PROGRAM 'true'" "ALTER TABLE app_user DISABLE TRIGGER ALL"; do
    if out=$(sql_as kaspisa_app "$stmt" 2>&1); then
        s9_bad "Т27: под kaspisa_app прошло: $stmt"
    elif [[ "$out" == *"permission denied"* || "$out" == *"must be owner"* ]]; then
        s9_ok "Т27: отказ в правах: $stmt"
    else
        s9_bad "Т27: отказ не по правам: $stmt"
    fi
done
if sql_as kaspisa_app "select count(*) from tenant" >/dev/null 2>&1; then
    s9_ok "Т27: SELECT под kaspisa_app проходит"
else
    s9_bad "Т27: SELECT под kaspisa_app не прошёл"
fi
s9_eq "Т27: log_error_verbosity" terse "$(sql_as postgres 'show log_error_verbosity')"
s9_cleanup
S9_DIR=

# --- Т62 ---
# init_failure ИМЯ ПОДСЕТЬ РЕЖИМ: копия, где после создания ролей CREATE ROLE kaspisa_app повторяется
# (ошибка «already exists»). РЕЖИМ=leak убирает из файла отключение журнала запросов (контроль).
init_failure() {
    s9_prepare "$1" local 13004 "$2"
    if [ "$3" = leak ]; then
        grep -v -e '^SET log_min_error_statement' -e '^SET log_statement' \
            "$S9_DIR/deploy/postgres/10-roles.sql" >"$S9_DIR/roles.tmp"
        mv "$S9_DIR/roles.tmp" "$S9_DIR/deploy/postgres/10-roles.sql"
    fi
    echo "CREATE ROLE kaspisa_app LOGIN PASSWORD :'app_password';" >>"$S9_DIR/deploy/postgres/10-roles.sql"
    s9_compose up -d --no-build db >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 30); do
        sleep 1
        s9_compose logs --no-log-prefix db 2>&1 | grep -q 'already exists' && break
    done
    sleep 2
    s9_compose logs --no-log-prefix db >"$S9_SCRATCH/dbfail-$1.log" 2>&1 || true
}

# leaks_in ФАЙЛ: 0, если в файле есть пароль или его подстрока длиной 12 (совпадения не печатаются).
leaks_in() {
    local name value i
    for name in POSTGRES_PASSWORD KASPISA_DB_OWNER_PASSWORD KASPISA_DB_APP_PASSWORD; do
        value=$(s9_env "$name")
        for ((i = 0; i + 12 <= ${#value}; i++)); do
            if grep -qF -- "${value:i:12}" "$1"; then return 0; fi
        done
    done
    return 1
}

s9_say "== Т62: сбой init"
init_failure dbfail 172.29.247.40/29 safe
log=$S9_SCRATCH/dbfail-dbfail.log
if grep -q 'ERROR' "$log" && grep -q 'already exists' "$log"; then s9_ok "Т62: в журнале ERROR и already exists"; else s9_bad "Т62: ошибки init нет в журнале"; fi
if leaks_in "$log"; then s9_bad "Т62: пароль или его часть в журнале"; else s9_ok "Т62: паролей в журнале нет"; fi
s9_cleanup
rm -f "$log"

s9_say "== Т62, контроль: без SET пароль попадает в журнал"
init_failure dbleak 172.29.247.48/29 leak
log=$S9_SCRATCH/dbfail-dbleak.log
if leaks_in "$log"; then s9_ok "Т62: контроль — без SET пароль виден (проверка чувствительна)"; else s9_bad "Т62: контроль не нашёл пароль, проверка не доказательна"; fi
s9_cleanup
rm -f "$log"

s9_finish
