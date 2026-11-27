#!/bin/bash
# Обновление kaspisa (ADR 0023, раздел 7 проекта): bash update.sh REF [--full-tests]
#   режим реестра  (KASPISA_IMAGE_PREFIX задан): REF — метка выпуска vX.Y.Z; образы с этой меткой
#                  скачиваются (`pull --policy missing`), файлы репозитория переключаются на метку;
#   режим сборки   (KASPISA_IMAGE_PREFIX пуст): REF — метка или ветка; `git merge`, `make lint`,
#                  `make test-backend` (с --full-tests — `make test`; порты тестовой базы:
#                  KASPISA_PG_PORT, KASPISA_REDIS_PORT) и сборка.
# Порядок: подготовка при работающей старой версии, остановка api и worker, копия базы, миграция,
# запуск новой версии (worker 60 с с KASPISA_DRY_RUN=true), запись версии в .env.
# Любой сбой после остановки откатывает к старой версии: git, а если миграция прошла — и база из
# копии этого запуска. Обрыв SSH (HUP) игнорируется; INT и TERM завершают скрипт с откатом.
# Вывод команд — в backups/update-<время>.log (права 600), в терминал — короткие сообщения.
# Запускайте в tmux. SIGKILL и выключение питания не ловятся: ручные шаги — в docs/deploy.md.
# Трассировка (set -x) запрещена: она напечатала бы секреты.
# SC2329: функции откат вызываются из ловушки on_exit, shellcheck этого не видит.
# shellcheck disable=SC2329
set -euo pipefail
umask 077
trap '' HUP PIPE

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
cd "$ROOT"
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

# Терминал владельца: fd 3 — stdout, fd 4 — stderr. Дальше stdout и stderr уходят в журнал.
exec 3>&1 4>&2

STAGE=start # start, prepare, stopped, changed, done
MODE=""
LOG_OPEN=""
LOG=""
OLD_HEAD=""
OLD_BRANCH=""
OLD_VERSION=""
OLD_REV=""
NEW_VERSION=""
PRE_DUMP=""
MIGRATE_FAILED=0
PUBLIC=""
SLEEP_PID=""
ENV_TRIED=""
ENV_WRITTEN=""

# Запись в терминал игнорирует ошибку: после обрыва SSH там EIO, и скрипт обязан дойти до конца.
say() {
    { printf '%s\n' "$*" >&3; } 2>/dev/null || true
    if [ -n "$LOG_OPEN" ]; then printf '%s\n' "$*"; fi
}
err() {
    { printf '%s\n' "$*" >&4; } 2>/dev/null || true
    if [ -n "$LOG_OPEN" ]; then printf '%s\n' "$*"; fi
}
fail() {
    err "update.sh: $*"
    exit 1
}

# Вызовы compose с новой версией. Значения передаются массивом KASPISA_COMPOSE_SET (см.
# kaspisa_compose в deploy/lib.sh), заданным в подоболочке: при выходе изнутри (set -e) он не
# остаётся видимым в ловушке EXIT, и откат поднимает старую версию из .env. Остальные KASPISA_*
# из оболочки владельца compose не получает.
compose_new() {
    (
        KASPISA_COMPOSE_SET=("KASPISA_VERSION=$NEW_VERSION")
        kaspisa_compose "$@"
    )
}
compose_new_dry() {
    (
        KASPISA_COMPOSE_SET=("KASPISA_VERSION=$NEW_VERSION" KASPISA_DRY_RUN=true)
        kaspisa_compose "$@"
    )
}

# alembic_version из базы (контейнер backup); пусто, если не прочиталась.
read_rev() {
    local rev=""
    rev=$(kaspisa_compose exec -T backup psql -X -Atc 'SELECT version_num FROM alembic_version' 2>/dev/null) || rev=""
    printf '%s' "${rev//[[:space:]]/}"
}

# Новая версия в .env: временный файл с правами 600 и mv; остальные строки не меняются.
write_env() {
    local tmp=$ROOT/.env.tmp.update.$$ line found=""
    ENV_TRIED=1
    {
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
            KASPISA_VERSION=*)
                printf 'KASPISA_VERSION=%s\n' "$NEW_VERSION"
                found=1
                ;;
            *) printf '%s\n' "$line" ;;
            esac
        done <"$ROOT/.env"
        if [ -z "$found" ]; then printf 'KASPISA_VERSION=%s\n' "$NEW_VERSION"; fi
    } >"$tmp" || {
        rm -f "$tmp"
        return 1
    }
    { chmod 600 "$tmp" && mv "$tmp" "$ROOT/.env"; } || {
        rm -f "$tmp"
        return 1
    }
    ENV_WRITTEN=1
}

# Возврат файлов репозитория к OLD_HEAD (слияние, если висит, отменяется).
git_back() {
    if git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then git merge --abort || return 1; fi
    if [ "$MODE" = registry ] && [ -z "$OLD_BRANCH" ]; then
        git checkout -q --detach "$OLD_HEAD" || return 1
    else
        if [ -n "$OLD_BRANCH" ]; then git checkout -q "$OLD_BRANCH" || return 1; fi
        git reset -q --hard "$OLD_HEAD" || return 1
    fi
}

# Откат по STAGE. Вызывается только из on_exit (set +e, INT и TERM игнорируются). Код: 1 — откат
# прошёл, 2 — откат сам упал.
rollback() {
    local restored="нет" cur
    say "Откат к версии $OLD_VERSION (этап: $STAGE)."
    if [ "$STAGE" = changed ]; then
        kaspisa_compose stop api worker web || true
        cur=$(read_rev)
        if [ "$MIGRATE_FAILED" = 1 ] && [ -n "$cur" ] && [ "$cur" = "$OLD_REV" ]; then
            say "Миграция не применилась (alembic_version $cur): база не восстанавливается."
        else
            if ! bash "$ROOT/deploy/restore.sh" --db-only "$ROOT/backups/$PRE_DUMP"; then
                rollback_failed
                return 2
            fi
            restored="да"
        fi
    fi
    if ! git_back; then
        rollback_failed
        return 2
    fi
    if [ "$STAGE" = stopped ] || [ "$STAGE" = changed ]; then
        if ! kaspisa_compose up -d --no-build --wait --wait-timeout 300; then
            rollback_failed
            return 2
        fi
        if [ -n "$PUBLIC" ]; then kaspisa_compose restart caddy || true; fi
    fi
    err "Обновление не удалось, работает прежняя версия $OLD_VERSION."
    if [ "$STAGE" = changed ]; then
        if [ "$restored" = да ]; then
            err "База восстановлена из копии backups/$PRE_DUMP."
        else
            err "База не восстанавливалась (копия этого запуска: backups/$PRE_DUMP)."
        fi
    elif [ "$STAGE" = stopped ]; then
        err "База не менялась."
    fi
    err "Подробности: $LOG"
    return 1
}

rollback_failed() {
    err "ОТКАТ НЕ УДАЛСЯ. api и worker могут быть остановлены."
    if [ -n "$PRE_DUMP" ]; then err "Копия перед обновлением: backups/$PRE_DUMP."; fi
    err "Ручные шаги — в docs/deploy.md. Журнал: $LOG"
}

# Единственная точка отката: любой выход с ошибкой после начала подготовки.
on_exit() {
    local rc=$? final
    trap '' INT TERM
    set +e
    final=$rc
    if [ -n "$SLEEP_PID" ]; then kill "$SLEEP_PID" 2>/dev/null; fi
    case "$STAGE" in
    start) ;;
    done)
        # Новая версия уже работает: сигнал после шага 8 откат не вызывает, только запись версии.
        if [ -z "$ENV_WRITTEN" ] && [ -z "$ENV_TRIED" ]; then
            if write_env; then
                final=0
            else
                err "Новая версия работает, но .env не обновлён: впишите KASPISA_VERSION=$NEW_VERSION вручную."
                final=2
            fi
        elif [ -n "$ENV_WRITTEN" ]; then
            final=0
        else
            # Запись версии прервана до mv: временный файл с секретами не оставляем.
            rm -f "$ROOT/.env.tmp.update.$$"
            err "Новая версия работает, но .env не обновлён: впишите KASPISA_VERSION=$NEW_VERSION вручную."
            final=2
        fi
        ;;
    *)
        if [ "$rc" -ne 0 ]; then
            rollback
            final=$?
        fi
        ;;
    esac
    kaspisa_lock_release
    if [ -n "$LOG_OPEN" ]; then say "итог: код $final"; fi
    exit "$final"
}

main() {
    local ref=${1:-} opt=${2:-} test_target=test-backend merge_ref tool root_name dev_project rc state cid out
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap on_exit EXIT

    # 1. Блокировка до любого другого действия: её сообщение идёт прямо в терминал.
    kaspisa_lock_acquire "$ROOT" update.sh || exit 1
    LOG=$ROOT/backups/update-$(date -u +%Y%m%dT%H%M%SZ).log
    : >"$LOG"
    exec >>"$LOG" 2>&1 </dev/null
    LOG_OPEN=1
    say "update.sh: журнал $LOG"

    [ -f .env ] || fail "нет .env: kaspisa не установлена (bash install.sh)"
    [ -n "$ref" ] || fail "укажите версию: bash update.sh vX.Y.Z (режим сборки: метка или ветка)"
    [[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "недопустимое имя версии"
    case "$opt" in
        "") ;;
        --full-tests) test_target="test" ;;
        *) fail "неизвестный параметр: $opt (допустим только --full-tests)" ;;
    esac
    [ "$#" -le 2 ] || fail "лишние параметры: bash update.sh vX.Y.Z [--full-tests]"
    if [ -n "$(kaspisa_env_get .env KASPISA_IMAGE_PREFIX)" ]; then
        MODE=registry
        [[ $ref =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "в режиме реестра версия — метка вида vX.Y.Z"
    else
        MODE=build
    fi
    docker info >/dev/null 2>&1 || fail "docker не работает"
    if [ -n "$(git status --porcelain)" ]; then
        fail "в рабочем дереве есть изменения (git status): зафиксируйте или отмените их"
    fi
    OLD_HEAD=$(git rev-parse HEAD)
    OLD_BRANCH=$(git symbolic-ref -q --short HEAD || true)
    OLD_VERSION=$(kaspisa_env_get .env KASPISA_VERSION)
    if [ "$(kaspisa_env_get .env COMPOSE_PROFILES)" = public ]; then PUBLIC=1; fi
    say "Обновление $OLD_VERSION -> $ref (режим: $MODE)."

    # 2. Подготовка: старая версия работает, откат — только git.
    STAGE=prepare
    git fetch --tags origin || fail "git fetch не удался"
    if [ "$MODE" = registry ]; then
        NEW_VERSION=$ref
        git checkout -q --detach "refs/tags/$ref" || fail "метки $ref нет в репозитории"
        compose_new pull --policy missing || fail "образы версии $ref не скачались"
    else
        # Инструменты сборки проверяются до слияния, пока ничего не изменено.
        for tool in uv node npm; do
            command -v "$tool" >/dev/null 2>&1 ||
                fail "не найден $tool: режиму сборки нужны uv, node и npm (docs/deploy.md, раздел «Что понадобится»)"
        done
        if [ -z "$(git config user.email || true)" ]; then
            export GIT_AUTHOR_NAME=kaspisa-update GIT_AUTHOR_EMAIL=update@kaspisa.invalid
            export GIT_COMMITTER_NAME=kaspisa-update GIT_COMMITTER_EMAIL=update@kaspisa.invalid
        fi
        # Ветка берётся с источника: локальная ветка после fetch не меняется.
        merge_ref=$ref
        if git show-ref --verify -q "refs/remotes/origin/$ref" &&
            ! git show-ref --verify -q "refs/tags/$ref"; then
            merge_ref=origin/$ref
        fi
        git merge --no-edit "$merge_ref" || fail "слияние с $ref не удалось (конфликт?)"
        make setup || fail "make setup не прошёл"
        make lint || fail "make lint не прошёл"
        # Тестовая база — отдельный проект dev-compose: общий .env с COMPOSE_PROJECT_NAME не должен
        # привести к тому же проекту, что у установки (make down убрал бы её).
        root_name=$(kaspisa_env_get .env COMPOSE_PROJECT_NAME)
        dev_project=${root_name:-kaspisa-prod}-update-test
        if ! COMPOSE_PROJECT_NAME=$dev_project COMPOSE_PROFILES='' make up; then
            COMPOSE_PROJECT_NAME=$dev_project COMPOSE_PROFILES='' make down || true
            fail "не поднялась тестовая база (make up)"
        fi
        rc=0
        # По умолчанию — тесты бэкенда (в них свойства «цена в границах»); полный набор с vitest и
        # e2e — по --full-tests для тех, кто правит код: случайные падения тестов интерфейса не должны
        # откатывать обновление у владельца.
        COMPOSE_PROJECT_NAME=$dev_project COMPOSE_PROFILES='' make "$test_target" || rc=$?
        COMPOSE_PROJECT_NAME=$dev_project COMPOSE_PROFILES='' make down || true
        [ "$rc" -eq 0 ] || fail "make $test_target не прошёл"
        NEW_VERSION=$(git describe --tags --always)
        compose_new build || fail "сборка образов не удалась"
    fi

    # 3. Сверка подсети edge и остановка: старый код не должен работать на новой схеме.
    kaspisa_check_edge_subnet .env || exit 1
    STAGE=stopped
    say "Останавливаю api и worker."
    kaspisa_compose stop worker api

    # 4. Копия базы этого запуска.
    OLD_REV=$(read_rev)
    # Класс update: своя ротация (3 копии), ручные копии владельца не вытесняются.
    out=$(kaspisa_compose exec -T backup bash /opt/kaspisa/deploy/backup.sh update 2>&1) || {
        printf '%s\n' "$out"
        fail "копия базы не снялась"
    }
    printf '%s\n' "$out"
    PRE_DUMP=$(printf '%s\n' "$out" | sed -n 's/.*создана копия \(update-[0-9]*T[0-9]*Z\.dump\)$/\1/p' | tail -n 1)
    [ -n "$PRE_DUMP" ] || fail "не удалось определить имя копии"
    STAGE=changed
    say "Копия базы: backups/$PRE_DUMP."

    # 5. Миграция новой версии.
    say "Миграция базы."
    if ! compose_new run --rm -T migrate; then
        MIGRATE_FAILED=1
        fail "миграция не прошла"
    fi

    # 6. Новые api и web; worker в окне проверки идёт с KASPISA_DRY_RUN=true.
    say "Запускаю api и web версии $NEW_VERSION."
    compose_new_dry up -d --no-build --wait --wait-timeout 180 api web
    if [ -n "$PUBLIC" ]; then kaspisa_compose restart caddy; fi

    # 7. Worker 60 с без записи цен в Kaspi: упал — откат ничего не теряет.
    say "Запускаю worker (60 с без записи цен)."
    compose_new_dry up -d --no-build worker
    sleep 60 &
    SLEEP_PID=$!
    wait "$SLEEP_PID" || true
    SLEEP_PID=""
    cid=$(kaspisa_compose ps -q worker)
    [ -n "$cid" ] || fail "контейнер worker не найден"
    state=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid")
    [ "$state" = "running 0" ] || fail "worker новой версии не работает ($state)"

    # backup и caddy тоже получают описание новой версии (образ, тома, порты). Это ещё сухое окно:
    # worker идёт с KASPISA_DRY_RUN=true, сбой или Ctrl-C здесь откатывает без потери записей цен.
    # Caddyfile лежит в смонтированном каталоге: не пересозданный `up` контейнер перечитывает его
    # после restart.
    compose_new_dry up -d --no-build backup
    if [ -n "$PUBLIC" ]; then
        compose_new_dry up -d --no-build caddy
        kaspisa_compose restart caddy
    fi

    # 8. Worker с настоящим значением KASPISA_DRY_RUN из .env (без префикса). После этого шага
    # откат с восстановлением базы невозможен: ничего, что может упасть, до записи версии нет.
    # Сигналы игнорируются уже во время `up worker` (сбой самой команды по-прежнему откатывает).
    trap '' INT TERM
    compose_new up -d --no-build worker
    STAGE='done'

    # 9. Запись версии.
    # Сбой записи: on_exit уберёт временный файл и скажет, что вписать вручную.
    write_env || exit 2
    say "Готово: работает версия $NEW_VERSION."
    say "Резервная копия перед обновлением: backups/$PRE_DUMP."
    return 0
}

main "$@"; exit $?
