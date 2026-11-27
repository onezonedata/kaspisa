.PHONY: setup lint test test-backend fmt up down dev-backend dev-worker dev-frontend show-key backup restore

setup:
	cd backend && uv sync
	cd frontend && npm ci && npx next typegen

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && npm run lint && npm run typecheck
	cd backend && uv run shellcheck -x -P SCRIPTDIR ../install.sh ../update.sh ../deploy/*.sh ../deploy/check/*.sh docker/start-api.sh
	cd backend && uv run actionlint ../.github/workflows/*.yml

test: test-backend
	cd frontend && npm test

test-backend:
	cd backend && uv run pytest -m "not live"

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .

up:
	docker compose up -d --wait

down:
	docker compose down

dev-backend:
	cd backend && uv run uvicorn kaspisa.main:app --reload --port 8000 --no-proxy-headers

dev-worker:
	cd backend && uv run python -m kaspisa.worker

dev-frontend:
	cd frontend && npm run dev

# Установка у владельца (docker-compose.prod.yml). show-key печатает только ключ шифрования:
# запускай в своём терминале, не через агента; вывод остаётся в прокрутке терминала и tmux.
show-key:
	@grep -E '^KASPISA_ENCRYPTION_KEY=' .env | tail -n 1 | cut -d= -f2-

backup:
	docker compose -f docker-compose.prod.yml exec -T backup bash /opt/kaspisa/deploy/backup.sh manual

restore:
	@test -n "$(FILE)" || { echo "укажите копию: make restore FILE=backups/<имя>.dump" >&2; exit 2; }
	bash deploy/restore.sh "$(FILE)"
