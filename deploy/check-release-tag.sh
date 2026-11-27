#!/bin/bash
# Сверка метки выпуска с версией проекта (release.yml, этап check).
#   bash deploy/check-release-tag.sh vX.Y.Z [путь/к/pyproject.toml]
# Метка должна быть вида vX.Y.Z и равняться "v" + version из pyproject.toml (по умолчанию
# backend/pyproject.toml рядом со скриптом). Иначе — сообщение в stderr и код 1.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TAG=${1-}
PYPROJECT=${2:-$ROOT/backend/pyproject.toml}

if [[ ! $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "метка '$TAG' не вида vX.Y.Z" >&2
    exit 1
fi

if [ ! -r "$PYPROJECT" ]; then
    echo "нет файла $PYPROJECT" >&2
    exit 1
fi

VERSION=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$PYPROJECT" | head -n 1)
if [ -z "$VERSION" ]; then
    echo "в $PYPROJECT не найдена версия" >&2
    exit 1
fi

if [ "$TAG" != "v$VERSION" ]; then
    echo "метка $TAG не совпадает с версией проекта v$VERSION" >&2
    exit 1
fi
