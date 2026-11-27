#!/usr/bin/env bash
# Хук PreToolUse для Bash: отклоняет команды, текст которых трогает секреты,
# запись цен или разрушающие операции. Фильтр по тексту, не граница безопасности:
# пропускает любую форму, которой нет в шаблонах. Зависимости: bash, sed -E, grep -E.
input=$(cat)
# Из JSON события берётся только поле command; при сбое проверяется весь ввод.
cmd=$(printf '%s' "$input" | sed -nE 's/.*"command":[[:space:]]*"(([^"\\]|\\.)*)".*/\1/p')
[ -n "$cmd" ] || cmd=$input
# Снятие экранирования JSON: перевод строки становится разделителем команд.
cmd=$(printf '%s' "$cmd" | sed -E -e 's/\\[nr]/;/g' -e 's/\\t/ /g' -e 's/\\"/"/g')
S='(^|[;&|(])[[:space:]]*(sudo[[:space:]]+)?' # начало отдельной команды

deny() {
  echo "kaspisa-guard: команда отклонена ($1), см. AGENTS.md; отдайте её владельцу" >&2
  exit 2
}
match() { printf '%s' "$cmd" | grep -Eq -- "$1"; }

match '\.env([^A-Za-z0-9_]|$)' && deny "А1: файл .env"
match '\.dump([^A-Za-z0-9_]|$)' && deny "А1: копия базы"
match "${S}[ef]?grep[[:space:]]([^;&|]*[[:space:]])?-([A-Za-z]*[rR][A-Za-z]*|-recursive|-dereference-recursive)([^A-Za-z-]|\$)" \
  && deny "А1: рекурсивный grep"
match 'environ([^A-Za-z]|$)' && deny "А6: окружение"
match 'printenv|export[[:space:]]+-p|(declare|typeset)[[:space:]]+-[A-Za-z]*[px]' && deny "А6: печать окружения"
match '(^|[^A-Za-z0-9_.-])env([^A-Za-z0-9_.-]|$)' && deny "А6: переменные окружения"
match "${S}(set|export|declare|typeset)[[:space:]]*([;&|)>]|\$)" && deny "А6: печать окружения"
match "${S}ps[[:space:]]+[A-Za-z]*e[A-Za-z]*([[:space:];&|]|\$)" && deny "А6: окружение процессов"
match 'DRY_RUN[[:space:]]*=' && deny "А3: запись цен"
match 'show-key' && deny "А7: ключ шифрования"
match 'kaspisa[./]api[./](add_user|reset_token)' && deny "А7: токен задания пароля"
match '(^|[^A-Za-z0-9_-])make[^;&|]*[[:space:]]restore([^A-Za-z0-9_-]|$)|restore\.sh|pg_restore' \
  && deny "А8: восстановление"
match 'docker[^;&|]*[[:space:]](config|convert|inspect|exec|run|cp|export|save|commit)([^A-Za-z0-9_-]|$)' \
  && deny "А7: docker"
exit 0
