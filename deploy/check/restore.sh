#!/bin/bash
# S9_NAME и S9_PROJECT читает common.sh (глобальные переменные проекта).
# shellcheck disable=SC2034
# Приёмка копий и восстановления: Т38, Т39, Т61, Т40 (часть).
# Запуск: S9_SCRATCH=<папка> bash deploy/check/restore.sh
# Два проекта: rs1 (копия и восстановление в нём) и rs2 (восстановление копии rs1 с ключом rs1).
# Секреты и ключи в вывод не попадают: только OK/FAIL и числа.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=common.sh
source ./common.sh
P1_DIR="" P2_DIR=""
use() { # 1 или 2: переключает глобальные переменные common.sh на проект
    if [ "$1" = 1 ]; then
        S9_DIR=$P1_DIR S9_NAME=rs1 S9_PROJECT=kaspisa-s9-rs1
    else
        S9_DIR=$P2_DIR S9_NAME=rs2 S9_PROJECT=kaspisa-s9-rs2
    fi
}
cleanup_all() {
    local n
    for n in 1 2; do
        [ -n "$([ "$n" = 1 ] && echo "$P1_DIR" || echo "$P2_DIR")" ] || continue
        use "$n"
        s9_cleanup
    done
}
trap cleanup_all EXIT

s9_prepare rs1 local 13010 172.29.247.64/29
P1_DIR=$S9_DIR
s9_prepare rs2 local 13011 172.29.247.72/29
P2_DIR=$S9_DIR
LOGS=$S9_SCRATCH/restore-logs
mkdir -p "$LOGS"

pass=$(s9_rand_hex | head -c 24)
json() { printf '{"email":"%s","password":"%s"}' "$1" "$pass"; }
api_post() { # ПУТЬ ПОЧТА → код ответа
    curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-Requested-With: kaspisa' \
        -H 'Content-Type: application/json' -d "$(json "$2")" "http://127.0.0.1:$PORT$1"
}
sql() { s9_compose exec -T db psql -U kaspisa -d kaspisa -Atc "$1"; }
# Хеш содержимого каждой таблицы public: таблица и md5 упорядоченной выборки строк.
table_hashes() {
    local t
    for t in $(sql "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"); do
        printf '%s %s\n' "$t" "$(sql "SELECT md5(coalesce(string_agg(x::text, E'\\n' ORDER BY x::text), '')) FROM public.\"$t\" x")"
    done
}
newest_manual() { find "$S9_DIR/backups" -maxdepth 1 -name 'manual-*.dump' | sort | tail -n 1; }
rotate_check() { s9_compose exec -T api python -m kaspisa.db.rotate_secrets --check >/dev/null 2>&1 && echo 0 || echo $?; }
leaks_in() { # ФАЙЛ: секреты текущего проекта
    if s9_leaks "$1"; then s9_bad "Т40: секрет в $(basename "$1")"; else s9_ok "Т40: $(basename "$1") без секретов"; fi
}

# ---- rs1: копия и восстановление в том же проекте (Т38)
use 1
PORT=13010
s9_say "== rs1: install.sh"
s9_install >"$LOGS/install-rs1.log" 2>&1 || {
    s9_bad "install.sh rs1 (журнал $LOGS/install-rs1.log)"
    exit 1
}
leaks_in "$LOGS/install-rs1.log"

s9_eq "Т38: регистрация A" 201 "$(api_post /api/auth/register a@example.test)"
tid=$(sql "SELECT tenant_id FROM app_user WHERE email='a@example.test'")
s9_compose stop worker >/dev/null 2>&1
s9_compose exec -T api python - "$tid" <"$S9_ROOT/deploy/check/seed_shop.py" >/dev/null
s9_eq "Т38: магазин A создан" 1 "$(sql "SELECT count(*) FROM shop")"
s9_eq "Т38: rotate_secrets --check до копии" 0 "$(rotate_check)"
(cd "$S9_DIR" && make backup) >"$LOGS/backup-rs1.log" 2>&1 || s9_bad "Т38: make backup"
leaks_in "$LOGS/backup-rs1.log"
dump=$(newest_manual)
if [ -n "$dump" ] && [ "$(stat_mode "$dump")" = 600 ]; then s9_ok "Т38: ручная копия есть, права 600"; else s9_bad "Т38: ручной копии нет или права не 600"; fi
hashes1=$(table_hashes)
s9_compose start worker >/dev/null 2>&1

s9_eq "Т38: регистрация B после копии" 201 "$(api_post /api/auth/register b@example.test)"
s9_eq "Т38: B входит до восстановления" 200 "$(api_post /api/auth/login b@example.test)"

(cd "$S9_DIR" && bash deploy/restore.sh --yes "backups/$(basename "$dump")") >"$LOGS/restore-rs1.log" 2>&1 ||
    s9_bad "Т38: restore.sh (журнал $LOGS/restore-rs1.log)"
leaks_in "$LOGS/restore-rs1.log"
s9_eq "Т38: после восстановления api и worker работают" 2 \
    "$(s9_compose ps -q --status running api worker | wc -l | tr -d ' ')"
s9_eq "Т38: вход A проходит" 200 "$(api_post /api/auth/login a@example.test)"
s9_eq "Т38: вход B — 401" 401 "$(api_post /api/auth/login b@example.test)"
s9_eq "Т38: регистрация C проходит (права kaspisa_app)" 201 "$(api_post /api/auth/register c@example.test)"
s9_eq "Т38: rotate_secrets --check после восстановления" 0 "$(rotate_check)"
s9_eq "Т38: kaspisa_app без владения таблицами" 0 \
    "$(sql "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner <> 'kaspisa'")"

# ---- Т61: блокировка в контейнере backup
s9_say "== Т61: блокировка копий в контейнере"
s9_compose exec -d -T backup flock /backups/.lock sleep 20
sleep 2
rm -f "$S9_DIR"/backups/daily-*.dump
rc=0
s9_compose exec -T backup bash /opt/kaspisa/deploy/backup.sh daily-if-due >/dev/null 2>&1 || rc=$?
s9_eq "Т61: при занятой блокировке код 0" 0 "$rc"
s9_eq "Т61: при занятой блокировке копии нет" 0 "$(find "$S9_DIR/backups" -maxdepth 1 -name 'daily-*.dump' | wc -l | tr -d ' ')"
sleep 20
rc=0
s9_compose exec -T backup bash /opt/kaspisa/deploy/backup.sh daily-if-due >/dev/null 2>&1 || rc=$?
s9_eq "Т61: после освобождения код 0" 0 "$rc"
s9_eq "Т61: после освобождения копия появилась" 1 "$(find "$S9_DIR/backups" -maxdepth 1 -name 'daily-*.dump' | wc -l | tr -d ' ')"

# ---- rs2: восстановление копии rs1 в другой проект (Т39)
s9_say "== rs2: восстановление в другой проект"
key1=$(s9_env KASPISA_ENCRYPTION_KEY)
use 2
PORT=13011
s9_install >"$LOGS/install-rs2.log" 2>&1 || {
    s9_bad "install.sh rs2 (журнал $LOGS/install-rs2.log)"
    exit 1
}
key2=$(s9_env KASPISA_ENCRYPTION_KEY)
set_key() { # КЛЮЧ: заменяет строку в .env проекта (права 600) и пересоздаёт api и worker
    (
        umask 077
        grep -v '^KASPISA_ENCRYPTION_KEY=' "$S9_DIR/.env" >"$S9_DIR/.env.new"
        printf 'KASPISA_ENCRYPTION_KEY=%s\n' "$1" >>"$S9_DIR/.env.new"
        mv "$S9_DIR/.env.new" "$S9_DIR/.env"
    )
    s9_compose up -d --no-build --wait --wait-timeout 300 api worker >/dev/null 2>&1
}
set_key "$key1"
use 1
src=$dump
use 2
cp "$src" "$S9_DIR/backups/"
(cd "$S9_DIR" && bash deploy/restore.sh --yes "backups/$(basename "$src")") >"$LOGS/restore-rs2.log" 2>&1 ||
    s9_bad "Т39: restore.sh rs2 (журнал $LOGS/restore-rs2.log)"
s9_compose stop worker >/dev/null 2>&1
hashes2=$(table_hashes)
s9_eq "Т39: хеши всех таблиц rs2 равны rs1" "$hashes1" "$hashes2"
s9_eq "Т39: вход A в rs2" 200 "$(api_post /api/auth/login a@example.test)"
s9_eq "Т39: rotate_secrets --check в rs2 с ключом rs1" 0 "$(rotate_check)"
if grep -qF -- "${key1#1:}" "$src"; then s9_bad "Т39: в копии есть ключ"; else s9_ok "Т39: в копии ключа нет"; fi
set_key "$key2"
rc=$(rotate_check)
# Ключи обоих проектов имеют идентификатор 1, поэтому чужой ключ даёт нечитаемые блоки (код 1);
# код 3 бывает, когда в связке нет ключа с нужным идентификатором.
if [ "$rc" = 1 ] || [ "$rc" = 3 ]; then s9_ok "Т39: с чужим ключом --check падает (код $rc)"; else s9_bad "Т39: с чужим ключом --check вернул $rc"; fi
leaks_in "$LOGS/install-rs2.log"
leaks_in "$LOGS/restore-rs2.log"

# ---- Т40: журналы сервисов обоих проектов
for n in 1 2; do
    use "$n"
    s9_compose logs --no-log-prefix >"$LOGS/compose-rs$n.txt" 2>&1 || true
    leaks_in "$LOGS/compose-rs$n.txt"
done
# ключ rs1 не должен встречаться в журналах rs2 и наоборот
use 1
if grep -qF -- "${key2#1:}" "$LOGS/compose-rs1.txt"; then s9_bad "Т40: ключ rs2 в журналах rs1"; else s9_ok "Т40: ключ rs2 в журналах rs1 не найден"; fi
rm -rf "$LOGS"

s9_finish
