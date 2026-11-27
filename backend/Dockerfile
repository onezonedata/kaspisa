# Образ kaspisa-backend: api, worker и migrate (команда задаётся в compose).
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3 UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
# Сначала зависимости: слой не пересобирается при правке кода.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin kaspisa
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv ./.venv
COPY alembic ./alembic
COPY docker/start-api.sh ./docker/start-api.sh
USER 10001
