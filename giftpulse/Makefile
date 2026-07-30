.PHONY: install test lint fmt check demo api bot indexer migrate revision drift up down logs clean

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

# What CI runs. Use this before pushing.
check: lint test drift

# One indexer cycle against mock venues, alerts evaluated but not sent.
demo:
	.venv/bin/giftpulse indexer --once --dry-run

indexer:
	.venv/bin/giftpulse indexer

api:
	.venv/bin/giftpulse api

bot:
	.venv/bin/giftpulse bot

# --- schema ---------------------------------------------------------------

migrate:
	.venv/bin/giftpulse migrate

# make revision m="add portfolio table"
revision:
	@test -n "$(m)" || (echo "usage: make revision m=\"what changed\"" && exit 1)
	.venv/bin/alembic revision --autogenerate -m "$(m)"

# Fails if models.py has changed without a matching revision.
drift:
	@GIFTPULSE_DATABASE_URL=sqlite:///./.drift.db .venv/bin/alembic upgrade head >/dev/null 2>&1 || true
	@GIFTPULSE_DATABASE_URL=sqlite:///./.drift.db .venv/bin/alembic check
	@rm -f .drift.db

# --- deployment -----------------------------------------------------------

up:
	docker compose --profile tls up -d --build

down:
	docker compose --profile tls down

logs:
	docker compose logs -f giftpulse

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.db *.db-wal *.db-shm .drift.db
